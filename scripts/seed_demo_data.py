"""시연용 데모 데이터 시딩·초기화 — Actions "Seed demo data"에서 실행 (작업 S-2).

실행: DATABASE_URL=... python scripts/seed_demo_data.py [--apply] [--reset-only] [--purge-test] [--purge-chat] [--attach-user ID]
- 기본은 미리보기: 무엇이 바뀔지만 출력하고 DB는 그대로 둔다
- --apply를 붙여야 실제로 저장한다 (한 번의 트랜잭션 — 중간에 실패하면 전부 되돌려짐)
- --reset-only: 데모 신고만 지운다 (새로 넣지 않음)
- --purge-test: 데모가 아닌 신고 전부(운영 DB의 테스트 신고)를 먼저 지운다. 되돌릴 수 없다.
  미리보기에서 지울 접수번호 목록을 보여준다. 다 지워지면 접수번호도 1번부터 다시 시작한다.
- --purge-chat: 개발·점검 중 쌓인 테스트 채팅(문의 로그 포함)을 지운다 — 데모가 아니고, 신고가 붙어 있지 않고,
  로그인 계정에 연결되지 않은 세션. --purge-test와 같이 쓰면 테스트 신고의 세션도 함께 지워진다. 되돌릴 수 없다.
- --attach-user ID: 넣은 데모 대화 중 8건을 그 학생 계정(사이트에서 가입한 login_id)에 연결한다 —
  그 계정으로 로그인하면 이전 대화 사이드바에 신고 대화가 채워진다. 계정을 만들지는 않는다(비밀번호를 코드에 둘 수 없음).
- 신고마다 대화 세션 1개와 대화 기록(접수 완료 안내·AI 판정 이유 포함)을 같이 넣는다 (S-2b).
형식·시나리오는 app/services/demo_data.py 참고.
"""
import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.session import get_sessionmaker
from app.models.enums import ReportStatus
from app.services import demo_data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="실제로 DB에 저장 (없으면 미리보기)")
    parser.add_argument("--reset-only", action="store_true", help="데모 신고만 지우고 새로 넣지 않음")
    parser.add_argument("--purge-test", action="store_true", help="데모가 아닌 신고 전부 삭제 (되돌릴 수 없음)")
    parser.add_argument(
        "--purge-chat", action="store_true", help="테스트 채팅(문의 로그 포함) 삭제 — 신고·로그인 계정이 붙은 세션은 남김 (되돌릴 수 없음)"
    )
    parser.add_argument(
        "--add-outage-demo",
        action="store_true",
        help="건물 전체 정전 시연 신고 8건을 기존 데이터 위에 추가 (아무것도 지우지 않음, 이미 있으면 건너뜀)",
    )
    parser.add_argument("--attach-user", default="", help="데모 대화 8건을 연결할 학생 계정의 login_id")
    args = parser.parse_args()
    attach_user = args.attach_user.strip()

    now = datetime.now(UTC)
    if args.add_outage_demo:
        return _add_outage_demo(args.apply, now)
    plan = demo_data.build_plan()
    db = get_sessionmaker()()
    try:
        existing = demo_data.count_demo(db)
        others = demo_data.list_non_demo_reports(db)
        mode = "저장함" if args.apply else "미리보기 (저장 안 함 — 저장하려면 apply 선택)"
        print(f"## 데모 데이터 — {mode}")
        print(f"- 지금 데모 신고: {existing}건 (있으면 지우고 새로 넣어요)")
        print(f"- 데모가 아닌 신고(테스트): {len(others)}건")
        if args.purge_test:
            nums = ", ".join(str(n) for n, _ in others) or "없음"
            print(f"- ⚠️ 삭제 대상 접수번호: {nums}")
            for n, d in others[:5]:
                print(f"  - {n}번: {d}…")
        else:
            print("- 테스트 신고는 그대로 둠 (지우려면 purge_test 선택)")
        chat_sessions, chat_messages = demo_data.count_orphan_chat(db)
        if args.purge_chat:
            note = " (테스트 신고도 함께 지우면 그 신고의 세션까지 늘어남)" if not args.purge_test else ""
            print(f"- ⚠️ 삭제 대상 테스트 채팅: 세션 {chat_sessions}개·메시지 {chat_messages}개{note}")
        else:
            print(f"- 테스트 채팅 {chat_sessions}개 세션은 그대로 둠 (지우려면 purge_chat 선택)")

        if attach_user:
            found = demo_data.find_user(db, attach_user) is not None
            print(f"- 학생 계정 '{attach_user}': {'있음 — 데모 대화 8건을 연결' if found else '없음 — 사이트에서 먼저 회원가입해야 연결돼요'}")
        if not args.apply:
            if not args.reset_only:
                _print_plan_summary(plan, now)
            return 0

        if args.purge_test:
            removed = demo_data.purge_non_demo(db)
            print(f"\n테스트 신고 {removed}건 삭제")
        if args.purge_chat:
            removed_chat = demo_data.purge_orphan_chat(db)
            print(f"테스트 채팅 세션 {removed_chat}개 삭제")
        removed_demo = demo_data.reset_demo(db)
        print(f"데모 신고 {removed_demo}건 삭제")
        if not args.reset_only:
            restarted = demo_data.restart_numbering_if_empty(db)
            if restarted:
                print("접수번호를 1번부터 다시 시작")
            counts = demo_data.insert_plan(db, plan, now)
            print(
                f"데모 신고 {counts.reports}건 입력 — 진행 중 {counts.open}건, 해결·종료 {counts.resolved_or_closed}건 "
                f"(SLA 준수 {counts.sla_met} / 초과 {counts.sla_breached})"
            )
            print(f"대화 세션 {counts.sessions}개·메시지 {counts.messages}개 입력 (관리자 상세의 '신고 대화'에서 보임)")
            if attach_user:
                n = demo_data.attach_demo_to_user(db, attach_user)
                if n:
                    print(f"학생 계정 '{attach_user}'에 데모 대화 {n}개 연결 (로그인하면 이전 대화 사이드바에 보임)")
                else:
                    print(f"⚠️ 학생 계정 '{attach_user}'를 찾지 못해 연결하지 못했어요 — 회원가입 후 attach_user만 넣고 apply를 다시 실행")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    if args.apply and not args.reset_only:
        print(
            "\n다음 단계: 탐지·예측 배치를 한 번 실행해야 문제 후보와 재발 예측이 채워져요 "
            "(ai `POST /api/v1/cron/detection-scan`, `prediction-update` 또는 Cloud Scheduler 강제 실행)."
        )
    return 0


def _add_outage_demo(apply: bool, now: datetime) -> int:
    """건물 전체 정전 시연 신고를 기존 데이터 위에 추가한다 (지우는 것 없음). 추가 뒤에는 탐지 배치를 실행."""
    from app.services import demo_outage

    specs = demo_outage.outage_plan()
    db = get_sessionmaker()()
    try:
        print(f"## 건물 전체 정전 시연 신고 — {'저장함' if apply else '미리보기 (저장 안 함)'}")
        if demo_outage.outage_exists(db):
            print("- 이미 들어 있어서 건너뜀")
            return 0
        for s in specs:
            print(f"- {s.building} {s.floor}층 {s.detail} · {s.category} · {s.priority} · {s.description}")
        if not apply:
            return 0
        counts = demo_data.insert_plan(db, specs, now)
        db.commit()
        print(f"\n신고 {counts.reports}건 추가 (진행 중 {counts.open}건). 다음: 탐지 배치(detection-scan) 실행")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return 0


def _print_plan_summary(plan: list[demo_data.DemoSpec], now: datetime) -> None:
    status_count: dict[str, int] = {}
    for spec in plan:
        status = demo_data.final_status(spec, now)
        status_count[status.value] = status_count.get(status.value, 0) + 1
    print(f"\n넣을 데모 신고: {len(plan)}건")
    for status in ReportStatus:
        print(f"- {status.value}: {status_count.get(status.value, 0)}건")


if __name__ == "__main__":
    sys.exit(main())
