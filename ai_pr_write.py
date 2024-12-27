"""
프로젝트: GitHub PR 본문 자동 작성

이 프로젝트는 GitHub Pull Request 생성 이벤트가 발생할 때,
1) PR 제목에서 노션 작업 ID를 추출하고 
2) 해당 노션 작업 ID에 해당하는 노션 페이지를 가져와서,
3) PR의 변경 사항과 노션 페이지의 내용을 LLM에 전달하여
4) PR 본문을 자동으로 작성하는 프로젝트입니다.
"""

import os
import re
import subprocess

import dotenv

from github import Github
from github.PullRequest import PullRequest

from notion_client import Client as NotionClient
from notion2md.exporter.block import StringExporter

from unidiff import PatchSet

from openai import OpenAI

dotenv.load_dotenv()


def main():
    # 0) Load environment variables
    github_token = os.getenv("GITHUB_TOKEN")
    repo_name = os.getenv("GITHUB_REPOSITORY")  # "owner/repo"
    pr_number_str = os.getenv("PR_NUMBER")      # e.g. "123"
    notion_token = os.getenv("NOTION_TOKEN")
    # e.g. "Always answer in Korean."
    system_prompt = os.getenv("SYSTEM_PROMPT")

    if not github_token or not repo_name or not pr_number_str or not notion_token or not system_prompt:
        raise EnvironmentError(
            "Missing one or more required environment variables: "
            "GITHUB_TOKEN, GITHUB_REPOSITORY, PR_NUMBER, NOTION_TOKEN, SYSTEM_PROMPT."
        )

    pr_number = int(pr_number_str)

    g = Github(github_token)
    pr = g.get_repo(repo_name).get_pull(pr_number)

    title = pr.title

    # 1) Extract Notion task ID from PR title
    notion = NotionClient(auth=notion_token)
    db_name_prefixes = extract_notion_db_name_prefixes(notion)

    # 2) Extract Task ID from PR title
    task_id = extract_dynamic_task_id(
        title, [prefix["prefix"] for prefix in db_name_prefixes])
    if not task_id:
        raise ValueError("No valid Notion Task ID found in the PR title.")
    print(f"Extracted Task ID: {task_id}")

    prefix = task_id.split("-")[0]
    number = int(task_id.split("-")[1])
    database_id, property_name = next(
        (db_name_prefix["database_id"], db_name_prefix["property_name"])
        for db_name_prefix in db_name_prefixes if db_name_prefix["prefix"].lower() == prefix.lower()
    )

    notion_page = search_page(notion, database_id, property_name, number)
    if not notion_page:
        raise ValueError(f"No Notion page found for Task ID: {task_id}")
    print(f"Fetched Notion Page ID: {notion_page['id']}")

    # 3) Fetch Notion page content
    notion_md = StringExporter(
        block_id=notion_page["id"], output_path="test").export()

    # 4) Get diff from PR
    patch_set = get_patchset_from_git(pr)
    patch_text = get_patch_text_from_patchset(patch_set)

    # 5) Write PR body
    pr_body = get_chatgpt_pr_body(
        patch_text, notion_md, pr, system_prompt
    )
    pr.edit(body=pr_body)



def extract_notion_db_name_prefixes(notion: NotionClient) -> list[dict]:
    """
    연결된 노션 계정의 모든 데이터베이스에서
    Unique ID 속성의 접두사를 추출합니다.

    Args:
        notion (NotionClient)

    Returns:
        [{
            "prefix": "TASK",
            "database_id": "12345678-1234-1234-1234-1234567890ab",
            "property_name": "ID"
        }]
    """
    databases = notion.search(
        filter={
            "value": "database",
            "property": "object"
        }
    )["results"]

    # select a property which type is unique_id
    return [
        {
            "prefix": property["unique_id"]["prefix"],
            "database_id": db["id"],
            "property_name": property["name"]
        } for db in databases for property in db["properties"].values() if property["type"] == "unique_id"
    ]


def extract_dynamic_task_id(title: str, prefixes: list[str]) -> str | None:
    """
    PR 제목에서 동적으로 Task ID를 추출합니다.

    Args:
        title (str): PR 제목
        prefixes (str): 데이터베이스 접두사의 리스트

    Returns:
        추출된 Task ID 또는 None
    """
    # 접두사를 포함한 정규식을 동적으로 생성
    pattern = r"(" + "|".join(re.escape(prefix)
                              for prefix in prefixes) + r")[\-\s](\d+)"
    match = re.search(pattern, title, re.IGNORECASE)
    if match:
        return f"{match.group(1).upper()}-{match.group(2)}"  # 예: TASK-1234
    return None


def search_page(notion: NotionClient, database_id: str, property_name: str, number: int) -> dict | None:
    """
    노션 페이지를 검색해옵니다.

    Args:
        notion (NotionClient)
        database_id (str): 노션 데이터베이스 ID
        property_name (str): Task ID를 저장하는 속성 이름
        number (int): 노션 페이지의 Task ID

    Returns:
        노션 페이지의 정보 또는 None
    """
    response = notion.databases.query(
        database_id=database_id,
        filter={
            "property": property_name,
            "unique_id": {
                "equals": number
            }
        }
    )

    results = response.get("results", [])
    if not results:
        return None
    return results[0]  # 첫 번째 매칭된 페이지 반환


def get_patchset_from_git(
    pr: PullRequest,
    context_lines: int = 3
) -> PatchSet:
    """
    'git diff --unified={context_lines} {base_ref}' 명령어를 실행해
    unified diff를 얻은 뒤, unidiff 라이브러리로 PatchSet 객체를 만들어 반환한다.

    Args:
        pr (PullRequest): The pull request object.
        context_lines (int): diff 생성 시 포함할 context 줄 수(기본 3줄)

    Returns:
        PatchSet: unidiff로 파싱된 diff 정보를 담은 PatchSet 객체
    """
    # GHA에서는 1001 사용자로 checkout 해주지만
    # Docker 사용자는 root 로 하길 권장합니다.
    # 따라서 safe.directory 설정이 필요합니다.
    # 그렇지 않으면 get diff 에서 not a git repository 에러가 발생합니다.
    result = subprocess.run(
        [
            'git',
            'config',
            '--global',
            '--add',
            'safe.directory',
            '/github/workspace'
        ],
        capture_output=True,
        text=True,
        check=False
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to run git config. Return code: {result.returncode}\n"
            f"stderr: {result.stderr}"
        )

    result = subprocess.run(
        [
            "git",
            "--no-pager",
            "diff",
            f"--unified={context_lines}",
            f"origin/{pr.base.ref}",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd="/github/workspace"
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to run git diff. Return code: {result.returncode}\n"
            f"stderr: {result.stderr}"
        )

    diff_text = result.stdout
    return PatchSet(diff_text)


def get_patch_text_from_patchset(
    patch_set: PatchSet,
    max_diff_lines: int = 1000
) -> str:
    patch_summary = []
    for patched_file in patch_set:
        patch_summary.append(f"File: {patched_file.path}")
        for hunk in patched_file:
            if len(hunk) > max_diff_lines:
                print(f"[WARN] Hunk too long for {patched_file.path}")
                patch_summary.append("Diff: [Too Long]")
                continue

            for line in hunk:
                if line.is_added:
                    patch_summary.append(
                        f"L{line.target_line_no}+ : {line.value.rstrip()}"
                    )
                elif line.is_removed:
                    patch_summary.append(
                        f"L{line.source_line_no}- : {line.value.rstrip()}"
                    )
                else:
                    patch_summary.append(
                        f"L{line.source_line_no} : {line.value.rstrip()}"
                    )

    return "\n".join(patch_summary)


def get_chatgpt_pr_body(
    patch_text: str,
    notion_md: str,
    pr: PullRequest,
    system_prompt: str,
) -> str:
    """
    Send patch_text + notion_md to ChatGPT(O1) (via openai) and return pr body.

    Args:
        patch_text (str): The unified diff text of the PR.
        notion_md (str): The markdown content of the Notion page.

    Returns:
        str: The generated PR body.
    """
    client = OpenAI()

    # 1) 프롬프트 생성
    prompt = (
        f"## Notion Document:\n{notion_md}\n\n"
        f"## PR Title:\n{pr.title}\n\n"
        f"## PR Body:\n{pr.body}\n\n"
        f"## Patch Diff:\n{patch_text}\n\n"
        f"Please write down a nice PR body from this PR."
    )

    # 2) ChatCompletion 호출
    response = client.chat.completions.create(
        model="o1",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a great software engineer. "
                    "Return only the PR body text. " + system_prompt
                )
            },
            {
                "role": "user",
                "content": prompt
            },
        ]
    )
    return json.loads(response.choices[0].message.content)['comments']


if __name__ == "__main__":
    main()
