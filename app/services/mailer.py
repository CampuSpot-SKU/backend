"""알림 메일 발송 — Gmail SMTP (명세 6장, 6-2 O-2). SLA 에스컬레이션(1-13)이 씀.

설정: SMTP_USER(보내는 Gmail)·SMTP_APP_PASSWORD(그 계정의 앱 비밀번호)·ALERT_EMAIL_TO(받는 주소, 쉼표로 여러 개).
하나라도 비어 있으면 MailNotConfigured — 부르는 쪽이 "메일 없이 진행"을 정한다.
"""
import logging
import smtplib
from email.message import EmailMessage

from app.config import get_settings

logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
TIMEOUT_SECONDS = 20


class MailNotConfigured(Exception):
    """SMTP 설정이 비어 있음."""


class MailError(Exception):
    """메일 서버 연결·인증·발송 실패."""


def recipients() -> list[str]:
    return [a.strip() for a in get_settings().alert_email_to.split(",") if a.strip()]


def send_alert(subject: str, body: str) -> int:
    """받는 사람 전원에게 같은 메일 한 통. 보낸 주소 수를 돌려줌."""
    s = get_settings()
    to = recipients()
    if not (s.smtp_user and s.smtp_app_password and to):
        raise MailNotConfigured("SMTP_USER·SMTP_APP_PASSWORD·ALERT_EMAIL_TO 중 비어 있는 값이 있어요")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"캠퍼스팟 알림 <{s.smtp_user}>"
    msg["To"] = ", ".join(to)
    msg.set_content(body)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=TIMEOUT_SECONDS) as smtp:
            smtp.starttls()
            smtp.login(s.smtp_user, s.smtp_app_password)
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(f"{type(e).__name__}: {e}") from e
    logger.info("알림 메일 발송: %s (%d명)", subject, len(to))
    return len(to)
