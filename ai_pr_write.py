"""
프로젝트: GitHub PR 본문 자동 작성

이 프로그램은 GitHub Pull Request에 대해 본문을 작성합니다.

다음 두 환경에서 실행됩니다.
- GitHub Actions: 단일 PR에 대해 실행
- 로컬 환경: 전체 PR에 대해 실행

1) PR 제목에서 노션 작업 ID를 추출하고 
2) 해당 노션 작업 ID에 해당하는 노션 페이지를 가져와서,
3) PR의 변경 사항과 노션 페이지의 내용을 LLM에 전달하여
4) PR 본문을 자동으로 작성하는 프로젝트입니다.
5) 로컬 환경인 경우, 사용자에게 덮어쓸지 확인하는 프로세스를 거칩니다.
"""

import os
import re
import subprocess
import sys
import datetime
import tempfile
import shutil

import dotenv
from github import Github
from github.PullRequest import PullRequest
from github.GithubException import UnknownObjectException

from notion_client import Client as NotionClient
from notion2md.exporter.block import StringExporter

from unidiff import PatchSet

from openai import OpenAI

dotenv.load_dotenv()


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
    git_dir: str,
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
        ['git', 'config', '--global', '--add', 'safe.directory', git_dir],
        capture_output=True,
        text=True,
        check=False
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to run git config. Return code: {result.returncode}\n"
            f"stderr: {result.stderr}"
        )

    print(f"pr.base.sha: {pr.base.sha}")
    result = subprocess.run(
        [
            'git',
            'fetch',
            'origin',
            pr.base.sha,
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=git_dir
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to run git fetch. Return code: {result.returncode}\n"
            f"stderr: {result.stderr}"
        )

    result = subprocess.run(
        [
            "git",
            "--no-pager",
            "diff",
            f"--unified={context_lines}",
            pr.base.sha,
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=git_dir
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
    max_diff_bytes: int = 10 * 1024  # 기본 10KB 제한, 필요에 따라 조정 가능
) -> str:
    patch_summary = []
    for patched_file in patch_set:
        patch_summary.append(f"File: {patched_file.path}")
        file_diff_lines = []
        # 각 파일의 모든 hunk의 라인 정보를 모아서 하나의 문자열로 생성
        for hunk in patched_file:
            for line in hunk:
                if line.is_added:
                    file_diff_lines.append(
                        f"L{line.target_line_no}+ : {line.value.rstrip()}"
                    )
                elif line.is_removed:
                    file_diff_lines.append(
                        f"L{line.source_line_no}- : {line.value.rstrip()}"
                    )
                else:
                    file_diff_lines.append(
                        f"L{line.source_line_no} : {line.value.rstrip()}"
                    )
        file_diff_text = "\n".join(file_diff_lines)
        # utf-8 인코딩 바이트 수 기준으로 크기 체크
        if len(file_diff_text.encode("utf-8")) > max_diff_bytes:
            print(f"[WARN] Diff too large for {patched_file.path}")
            patch_summary.append("Diff: [Too Long]")
        else:
            patch_summary.append(file_diff_text)
    return "\n".join(patch_summary)


def get_chatgpt_pr_body(
    patch_text: str,
    notion_md: str | None,
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
    prompt_lines = []
    if notion_md:
        prompt_lines.append(f"# Notion Document:\n{notion_md}")
        prompt_lines.append("----\n\n")
    prompt_lines += [
        f"# PR Title:\n{pr.title}\n\n",
        "----\n\n",
        f"# PR Body:\n{pr.body}\n\n",
        "----\n\n",
        f"# Patch Diff:\n"
        "_L13+ : This line was added in the PR._\n"
        "_L13- : This line was removed in the PR._\n"
        "_L13 : This line was unchanged in the PR._\n"
        f"{patch_text}\n\n"
        "----\n\n",
        "Please write down a nice PR body from this PR."
    ]
    prompt = "".join(prompt_lines)

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
    return response.choices[0].message.content


def generate_pr_body(pr: PullRequest, notion_token: str, system_prompt: str, git_dir: str) -> str:
    """
    PR 본문 생성을 위한 전체 프로세스를 실행합니다.
    """
    # 1) 노션 페이지 내용 가져오기
    notion = NotionClient(auth=notion_token)
    db_prefixes = extract_notion_db_name_prefixes(notion)
    task_id = extract_dynamic_task_id(
        pr.title, [p["prefix"] for p in db_prefixes])
    notion_md = None
    if task_id:
        prefix, num_str = task_id.split("-")
        number = int(num_str)
        for item in db_prefixes:
            if item["prefix"].lower() == prefix.lower():
                database_id = item["database_id"]
                property_name = item["property_name"]
                notion_page = search_page(
                    notion, database_id, property_name, number)
                if notion_page:
                    print(f"Notion 페이지 ID: {notion_page['id']} 조회됨.")
                    notion_md = StringExporter(
                        block_id=notion_page["id"]).export()
                else:
                    print(f"Task ID {task_id}에 해당하는 Notion 페이지를 찾을 수 없습니다.")
                break
    else:
        print("PR 제목에서 유효한 Task ID를 찾지 못했습니다.")

    # 2) git diff 추출
    patch_set = get_patchset_from_git(pr, git_dir)
    patch_text = get_patch_text_from_patchset(patch_set)
    print(patch_text)

    # 3) AI로 PR 본문 생성
    ai_pr_body = get_chatgpt_pr_body(patch_text, notion_md, pr, system_prompt)
    return ai_pr_body


def confirm_overwrite(existing_body: str, new_body: str) -> bool:
    """
    기존 PR 본문과 새로 생성된 PR 본문을 출력하고,
    덮어쓸지 사용자에게 확인합니다.
    """
    print("\n====== 기존 PR 본문 ======")
    print(existing_body)
    print("\n====== AI로 생성된 새 PR 본문 ======")
    print(new_body)
    choice = input("\n이 PR 본문으로 덮어쓰시겠습니까? (y/n): ").strip().lower()
    return choice == "y"


def process_single_pr(
    pr: PullRequest,
    notion_token: str,
    system_prompt: str,
    label_name: str,
    git_dir: str,
    need_confirm: bool = False
):
    """
    하나의 PR에 대해 AI 본문 생성 및 덮어쓰기 작업을 수행합니다.
    """
    print(f"\nProcessing PR #{pr.number}: {pr.title}")
    ai_body = generate_pr_body(pr, notion_token, system_prompt, git_dir)
    if not need_confirm or confirm_overwrite(pr.body, ai_body):
        pr.edit(body=ai_body)
        repo = pr.base.repo
        try:
            label = repo.get_label(label_name)
        except UnknownObjectException:
            label = repo.create_label(
                name=label_name, color="f29513", description="PR body auto-generated by AI."
            )
        pr.add_to_labels(label)
        print(f"PR #{pr.number} 본문이 업데이트되었습니다.")
    else:
        print(f"PR #{pr.number} 본문 업데이트가 취소되었습니다.")


# ---------- 단일 PR 및 전체 PR 처리 함수 ----------

def process_single_pr_from_env():
    """
    환경 변수에서 단일 PR 정보를 읽어와 처리합니다.
    (GITHUB_TOKEN, GITHUB_REPOSITORY, PR_NUMBER, NOTION_TOKEN 필요)
    """
    github_token = os.getenv("GITHUB_TOKEN")
    repo_name = os.getenv("GITHUB_REPOSITORY")
    pr_number_str = os.getenv("PR_NUMBER")
    notion_token = os.getenv("NOTION_TOKEN")
    system_prompt = os.getenv("SYSTEM_PROMPT") or ""
    label_name = os.getenv("LABEL") or "ai-pr-written"

    if not github_token or not repo_name or not pr_number_str or not notion_token:
        raise EnvironmentError(
            "GITHUB_TOKEN, GITHUB_REPOSITORY, PR_NUMBER, NOTION_TOKEN 환경 변수가 필요합니다.")

    pr_number = int(pr_number_str)
    g = Github(github_token)
    repo = g.get_repo(repo_name)
    pr = repo.get_pull(pr_number)
    # non-batch 모드에서는 기존 경로 사용
    git_dir = "/github/workspace"
    process_single_pr(pr, notion_token, system_prompt, label_name, git_dir)


def process_all_prs():
    """
    특정 레포지토리의 모든 열려있는 PR 중
    ai-pr-written 태그가 없는 PR에 대해 처리를 수행합니다.
    """
    github_token = os.getenv("GITHUB_TOKEN")
    repo_name = os.getenv("GITHUB_REPOSITORY")
    notion_token = os.getenv("NOTION_TOKEN")
    system_prompt = os.getenv("SYSTEM_PROMPT") or ""
    label_name = os.getenv("LABEL") or "ai-pr-written"

    if not github_token or not repo_name or not notion_token:
        raise EnvironmentError(
            "GITHUB_TOKEN, GITHUB_REPOSITORY, NOTION_TOKEN 환경 변수가 필요합니다.")

    g = Github(github_token)
    repo = g.get_repo(repo_name)

    open_prs = repo.get_pulls(state="all", sort="created", direction="desc")
    for pr in open_prs:
        # ai-pr-written 라벨이 이미 있으면 건너뜁니다.
        if any(label.name == label_name for label in pr.get_labels()):
            print(f"PR #{pr.number}은 이미 '{label_name}' 라벨이 있으므로 건너뜁니다.")
            continue

        # 최근 6개월 이내 PR만 대상으로 합니다.
        created_at = pr.created_at
        now = datetime.datetime.now(datetime.timezone.utc)
        if (now - created_at).days > 180:
            print(f"PR #{pr.number}은 최근 6개월 이내에 업데이트된 PR가 아니므로 건너뜁니다.")
            continue

        # 해당 PR에 대해 clone을 수행합니다.
        dest_dir = tempfile.mkdtemp(prefix="git_repo_")
        print(
            f"Cloning repository {repo_name} into temporary directory {dest_dir}...")

        repo = pr.base.repo
        clone_url = repo.clone_url
        pr_number = pr.number
        # 1. 레포지토리 clone
        print(f"Cloning repository {repo.full_name} into {dest_dir}...")
        result = subprocess.run(
            ["git", "clone", clone_url, dest_dir],
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to clone repository: {result.stderr}")

        # 2. PR의 ref(fetch) – PR 번호에 해당하는 ref를 로컬 브랜치로 생성
        fetch_command = ["git", "fetch", "origin",
                         f"pull/{pr_number}/head:pr-{pr_number}"]
        print(f"Fetching PR branch with command: {' '.join(fetch_command)}")
        result = subprocess.run(
            fetch_command,
            cwd=dest_dir,
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to fetch PR branch: {result.stderr}")

        # 3. 생성된 브랜치 체크아웃
        checkout_command = ["git", "checkout", f"pr-{pr_number}"]
        print(
            f"Checking out branch with command: {' '.join(checkout_command)}")
        result = subprocess.run(
            checkout_command,
            cwd=dest_dir,
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to checkout branch: {result.stderr}")

        print(f"Successfully cloned and checked out PR #{pr_number} branch.")

        process_single_pr(pr, notion_token, system_prompt,
                          label_name, dest_dir, True)

        # 작업 완료 후 임시 디렉토리 삭제
        shutil.rmtree(dest_dir)


# ---------- 실행 진입점 ----------

if __name__ == "__main__":
    # 명령행 인자로 "batch"가 주어지면 전체 PR 처리, 없으면 단일 PR 처리
    if len(sys.argv) > 1 and sys.argv[1] == "batch":
        process_all_prs()
    else:
        process_single_pr_from_env()
