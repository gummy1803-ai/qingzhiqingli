"""邮件报警模块:发现高危数据时发送邮件通知。

SMTP 配置从 config/email_config.ini 读取(基于 .ini 格式,避免依赖 dotenv)。
模板见 config/email_config.example.ini,复制后填入实际值即可启用。
"""
from __future__ import annotations

import configparser
import logging
import smtplib
import ssl
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from src.log_config import get_logger

logger = get_logger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "email_config.ini"


def load_smtp_config(config_path: Path | str | None = None) -> dict | None:
    """读取 SMTP 配置文件。

    Returns:
        dict: 配置字典; None: 配置文件不存在或未配置完整
    """
    cfg_path = Path(config_path) if config_path else CONFIG_PATH
    if not cfg_path.exists():
        logger.warning("邮件配置不存在: %s (复制 .example.ini 启用)", cfg_path)
        return None

    cp = configparser.ConfigParser()
    cp.read(cfg_path, encoding="utf-8")
    if "smtp" not in cp:
        return None

    cfg = {
        "host": cp.get("smtp", "host", fallback=""),
        "port": cp.getint("smtp", "port", fallback=465),
        "user": cp.get("smtp", "user", fallback=""),
        "password": cp.get("smtp", "password", fallback=""),
        "to": cp.get("smtp", "to", fallback=""),
        "from_name": cp.get("smtp", "from_name", fallback="设备数据助手"),
    }
    if not (cfg["host"] and cfg["user"] and cfg["password"] and cfg["to"]):
        logger.warning("邮件配置不完整,请填写 host/user/password/to")
        return None
    return cfg


def send_alert(subject: str, body: str,
               attachment: Path | str | None = None,
               config_path: Path | str | None = None) -> bool:
    """发送报警邮件。

    Args:
        subject: 邮件主题
        body: 邮件正文(纯文本)
        attachment: 可选附件路径(如质量简报 .txt 或 .csv)
        config_path: 配置文件路径,默认 config/email_config.ini

    Returns:
        bool: True=发送成功, False=失败或未配置
    """
    cfg = load_smtp_config(config_path)
    if cfg is None:
        logger.warning("邮件报警跳过: 配置未启用")
        return False

    try:
        msg = MIMEMultipart()
        msg["From"] = f"{cfg['from_name']} <{cfg['user']}>"
        msg["To"] = cfg["to"]
        msg["Subject"] = subject

        msg.attach(MIMEText(body, "plain", "utf-8"))

        # 附件
        if attachment:
            att_path = Path(attachment)
            if att_path.exists():
                with att_path.open("rb") as f:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(f.read())
                    encoders.encode_base64(part)
                    part.add_header(
                        "Content-Disposition",
                        f'attachment; filename="{att_path.name}"',
                    )
                    msg.attach(part)
                logger.info("附件已附加: %s", att_path.name)

        # 发送
        context = ssl.create_default_context()
        if cfg["port"] == 465:
            # SSL 直连
            with smtplib.SMTP_SSL(cfg["host"], cfg["port"],
                                  context=context, timeout=15) as s:
                s.login(cfg["user"], cfg["password"])
                s.sendmail(cfg["user"], cfg["to"].split(","), msg.as_string())
        else:
            # 587 走 STARTTLS
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as s:
                s.ehlo()
                s.starttls(context=context)
                s.ehlo()
                s.login(cfg["user"], cfg["password"])
                s.sendmail(cfg["user"], cfg["to"].split(","), msg.as_string())

        logger.info("邮件报警已发送: %s → %s", cfg["user"], cfg["to"])
        return True

    except Exception as e:
        logger.error("邮件发送失败: %s", e, exc_info=True)
        return False
