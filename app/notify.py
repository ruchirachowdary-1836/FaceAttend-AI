"""Absentee alerts to guardians. Real e-mail when SMTP_HOST is set, otherwise a dry run."""
import smtplib
from email.message import EmailMessage

from .config import SMTP_FROM, SMTP_HOST, SMTP_PASSWORD, SMTP_PORT, SMTP_USER


def send_absentee_mails(day, absentees):
    """absentees: dicts with name, roll_no, guardian_email. Returns a per-recipient result list."""
    results, targets = [], [a for a in absentees if a.get("guardian_email")]
    smtp = None
    if SMTP_HOST and targets:
        smtp = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
        smtp.starttls()
        if SMTP_USER:
            smtp.login(SMTP_USER, SMTP_PASSWORD)
    try:
        for a in targets:
            msg = EmailMessage()
            msg["Subject"] = f"Attendance alert: {a['name']} was absent on {day}"
            msg["From"], msg["To"] = SMTP_FROM, a["guardian_email"]
            msg.set_content(
                f"Dear Guardian,\n\n{a['name']} (Roll No. {a['roll_no']}) was marked absent on {day}.\n"
                "Please contact the department if this is unexpected.\n\n- FaceAttend")
            try:
                if smtp:
                    smtp.send_message(msg)
                results.append({"to": a["guardian_email"], "student": a["name"],
                                "status": "sent" if smtp else "dry-run"})
            except Exception as exc:
                results.append({"to": a["guardian_email"], "student": a["name"],
                                "status": f"failed: {exc}"})
    finally:
        if smtp:
            smtp.quit()
    return results
