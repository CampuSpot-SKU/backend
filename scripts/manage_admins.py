"""관리자 계정 만들기·비밀번호 교체 — Actions "Manage admins"에서 실행 (작업 S-3).

실행: DATABASE_URL=... ADMIN_ACCOUNTS='[...]' python scripts/manage_admins.py [--apply]
- 기본은 미리보기(dry run): 무엇이 바뀔지만 출력하고 DB는 그대로 둔다
- --apply를 붙여야 실제로 저장한다
- 비밀번호 값은 출력하지 않는다. 형식은 app/services/admin_accounts.py 맨 위 설명 참고
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.session import get_sessionmaker
from app.services.admin_accounts import AccountInputError, apply_accounts, parse_accounts

ACTION_LABEL = {"create": "새로 만듦", "update": "비밀번호 교체"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="실제로 DB에 저장 (없으면 미리보기)")
    args = parser.parse_args()

    raw = os.environ.get("ADMIN_ACCOUNTS", "")
    if not raw.strip():
        print("ADMIN_ACCOUNTS가 비어 있어요 — backend 레포 Secret을 먼저 등록하세요.")
        return 2
    try:
        specs = parse_accounts(raw)
        db = get_sessionmaker()()
        try:
            result = apply_accounts(db, specs, dry_run=not args.apply)
        finally:
            db.close()
    except AccountInputError as e:
        print(f"입력 오류: {e}")
        return 2

    mode = "저장함" if args.apply else "미리보기 (저장 안 함 — 저장하려면 apply 선택)"
    print(f"## 관리자 계정 — {mode}")
    for c in result.changes:
        print(f"- {c.login_id}: {ACTION_LABEL[c.action]}")
    if result.still_public:
        print(
            "\n⚠️ 아직 공개된 초기 비밀번호를 쓰는 계정: "
            + ", ".join(result.still_public)
            + " — ADMIN_ACCOUNTS에 넣어 교체하세요."
        )
        return 1 if args.apply else 0
    print("\n공개된 초기 비밀번호를 쓰는 계정 없음.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
