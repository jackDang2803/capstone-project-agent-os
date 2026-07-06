# app/email_client.py
import email
import imaplib
import re
import urllib.parse
from email.header import decode_header
from typing import Any

import requests
from bs4 import BeautifulSoup

from app.config import Config


class EmailClient:
    @staticmethod
    def get_unread_emails() -> list[dict[str, Any]]:
        """Connects to Gmail and fetches all unread emails."""
        if not Config.GMAIL_EMAIL or not Config.GMAIL_PASSWORD:
            print("Gmail credentials are not configured. Returning empty list.")
            return []

        emails_data = []
        try:
            # Connect to imap.gmail.com
            mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
            mail.login(Config.GMAIL_EMAIL, Config.GMAIL_PASSWORD)
            mail.select("inbox")

            # Search for all unread emails
            status, messages = mail.search(None, "UNSEEN")
            if status != "OK" or not messages[0]:
                mail.close()
                mail.logout()
                return []

            mail_ids = messages[0].split()
            print(f"Found {len(mail_ids)} unread emails.")

            for mail_id in mail_ids:
                status, msg_data = mail.fetch(mail_id, "(RFC822)")
                if status != "OK":
                    continue

                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        # Parse bytes email into Message object
                        msg = email.message_from_bytes(response_part[1])

                        # Decode subject
                        subject, encoding = decode_header(msg["Subject"])[0]
                        if isinstance(subject, bytes):
                            subject = subject.decode(
                                encoding or "utf-8", errors="ignore"
                            )

                        # Decode sender
                        sender, encoding = decode_header(msg["From"])[0]
                        if isinstance(sender, bytes):
                            sender = sender.decode(encoding or "utf-8", errors="ignore")

                        # Extract body
                        body = ""
                        if msg.is_multipart():
                            for part in msg.walk():
                                content_type = part.get_content_type()
                                content_disposition = str(
                                    part.get("Content-Disposition")
                                )

                                if (
                                    content_type == "text/plain"
                                    and "attachment" not in content_disposition
                                ):
                                    payload = part.get_payload(decode=True)
                                    body += payload.decode(
                                        part.get_content_charset() or "utf-8",
                                        errors="ignore",
                                    )
                                elif (
                                    content_type == "text/html"
                                    and "attachment" not in content_disposition
                                    and not body
                                ):
                                    # Fallback to HTML if no plain text
                                    payload = part.get_payload(decode=True)
                                    html_content = payload.decode(
                                        part.get_content_charset() or "utf-8",
                                        errors="ignore",
                                    )
                                    # Extract text from HTML
                                    soup = BeautifulSoup(html_content, "html.parser")
                                    body += soup.get_text()
                        else:
                            content_type = msg.get_content_type()
                            payload = msg.get_payload(decode=True)
                            charset = msg.get_content_charset() or "utf-8"
                            if content_type == "text/plain":
                                body = payload.decode(charset, errors="ignore")
                            elif content_type == "text/html":
                                soup = BeautifulSoup(
                                    payload.decode(charset, errors="ignore"),
                                    "html.parser",
                                )
                                body = soup.get_text()

                        emails_data.append(
                            {
                                "id": mail_id,
                                "subject": subject or "No Subject",
                                "sender": sender or "Unknown Sender",
                                "body": body.strip(),
                            }
                        )

                        # Mark email as read / SEEN
                        mail.store(mail_id, "+FLAGS", "\\Seen")

            mail.close()
            mail.logout()
        except Exception as e:
            print(f"Error fetching emails: {e}")

        return emails_data

    @staticmethod
    def extract_urls(text: str) -> list[str]:
        """Extracts absolute HTTP/HTTPS URLs from text."""
        url_pattern = r"https?://[^\s<>\"']+"
        urls = re.findall(url_pattern, text)

        # Clean trailing punctuation
        cleaned_urls = []
        for url in urls:
            while url and url[-1] in [".", ",", ")", "(", "!", "?", ";", ":", "/"]:
                # Keep ending slash if it's part of domain, else strip
                if url[-1] == "/" and url.count("/") <= 3:
                    break
                url = url[:-1]
            if url and url not in cleaned_urls:
                # Filter out obvious unsubscribe/social links
                if not any(
                    term in url.lower()
                    for term in [
                        "unsubscribe",
                        "optout",
                        "privacy",
                        "terms",
                        "facebook.com",
                        "twitter.com",
                        "linkedin.com",
                        "instagram.com",
                    ]
                ):
                    cleaned_urls.append(url)
        return cleaned_urls

    @staticmethod
    def scrape_url(url: str) -> dict[str, str]:
        """Fetches URL and extracts title and clean readable text."""
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()

            soup = BeautifulSoup(response.content, "html.parser")

            # Extract title
            title = soup.title.string.strip() if soup.title else ""
            if not title:
                title = urllib.parse.urlparse(url).netloc

            # Remove script and style elements
            for script in soup(["script", "style", "nav", "footer", "header"]):
                script.decompose()

            # Extract text
            paragraphs = soup.find_all(["p", "h1", "h2", "h3", "h4", "article"])
            text = "\n\n".join(
                [p.get_text().strip() for p in paragraphs if p.get_text().strip()]
            )

            # Normalize whitespace
            text = re.sub(r"\n{3,}", "\n\n", text)

            return {
                "title": title,
                "url": url,
                "content": text[:15000],  # Limit content length
            }
        except Exception as e:
            print(f"Error scraping {url}: {e}")
            return {
                "title": urllib.parse.urlparse(url).netloc,
                "url": url,
                "content": f"Failed to fetch webpage contents from {url}.",
            }

    @classmethod
    def process_email_to_resources(
        cls, email_item: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Extracts resources from email body.

        If links are found, scrape them and return as resources.
        If no links, return the email body itself as a resource.
        """
        body = email_item["body"]
        urls = cls.extract_urls(body)
        resources = []

        if urls:
            print(
                f"Extracted {len(urls)} links from email '{email_item['subject']}'. Scraping them..."
            )
            for url in urls:
                scraped = cls.scrape_url(url)
                resources.append(
                    {
                        "title": scraped["title"],
                        "url": scraped["url"],
                        "source_email_subject": email_item["subject"],
                        "source_email_sender": email_item["sender"],
                        "content": scraped["content"],
                    }
                )
        else:
            print(
                f"No links found in email '{email_item['subject']}'. Using email body as content."
            )
            resources.append(
                {
                    "title": email_item["subject"],
                    "url": "",
                    "source_email_subject": email_item["subject"],
                    "source_email_sender": email_item["sender"],
                    "content": body,
                }
            )

        return resources
