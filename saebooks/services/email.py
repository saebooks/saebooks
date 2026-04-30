"""Email service with template rendering.

Wraps the mailer module to add Jinja2 template rendering support.
Templates are located in saebooks/templates/emails/
"""
from __future__ import annotations

import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from saebooks.config import settings
from saebooks.services.mailer import EmailAttachment, EmailResult, send_email as mailer_send_email

logger = logging.getLogger(__name__)

# Initialize Jinja2 environment for email templates
TEMPLATES_DIR = Path(__file__).parent.parent / "templates" / "emails"
jinja_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=True,
)


async def send_email(
    to: str | list[str],
    subject: str,
    template: str,
    context: dict | None = None,
    *,
    text: str | None = None,
    attachments: list[EmailAttachment] | None = None,
    sender: str | None = None,
) -> EmailResult:
    """Send email using a Jinja2 template.
    
    Args:
        to: Recipient email address or list of addresses
        subject: Email subject line
        template: Template name (without .html extension, e.g., "magic_link_email")
        context: Context dictionary for template rendering
        text: Optional plain-text fallback (auto-generated if not provided)
        attachments: List of EmailAttachment objects
        sender: Optional sender address (defaults to settings.smtp_from)
    
    Returns:
        EmailResult with mode ("smtp" or "outbox"), message_id, and recipients
    
    Raises:
        EmailError: On SMTP failure or file I/O errors
        FileNotFoundError: If template doesn't exist
        jinja2.TemplateError: On template rendering errors
    """
    context = context or {}
    
    # Render HTML from template
    template_path = f"{template}.html"
    try:
        tmpl = jinja_env.get_template(template_path)
        html = tmpl.render(**context)
    except FileNotFoundError as e:
        logger.error(f"Email template not found: {template_path}")
        raise
    except Exception as e:
        logger.error(f"Error rendering email template {template_path}: {e}")
        raise
    
    # Send via mailer
    result = await mailer_send_email(
        to=to,
        subject=subject,
        html=html,
        text=text,
        attachments=attachments,
        sender=sender,
        settings=settings,
    )
    
    return result
