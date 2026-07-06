# app/agent.py
import json
import os
import re
from typing import Any

from google.adk import Context, Event
from google.adk.apps import App
from google.adk.events.event_actions import EventActions
from google.adk.workflow import START, Edge, Workflow, node
from google.genai import Client, types

from app.config import Config
from app.database import Database
from app.email_client import EmailClient


def make_event(message: str, state: dict[str, Any] | None = None) -> Event:
    content = types.Content(parts=[types.Part.from_text(text=message)])
    actions = EventActions(state_delta=state) if state else EventActions()
    return Event(content=content, actions=actions)


# Initialize database tables
Database.init_db()


def get_genai_client() -> Client:
    """Returns a google-genai Client initialized with configured API key."""
    # Ensure GEMINI_API_KEY is in the environment
    if Config.GEMINI_API_KEY:
        os.environ["GEMINI_API_KEY"] = Config.GEMINI_API_KEY
    return Client()


@node
def fetch_emails_node(ctx: Context, node_input: Any) -> Event:
    """Node 1: Securely polls Gmail and scrapes webpage contents of any links."""
    print("Executing fetch_emails_node...")

    # In a real environment, this connects to Gmail.
    # If credentials are not set, it returns an empty list.
    emails = EmailClient.get_unread_emails()

    all_raw_resources = []
    for email_item in emails:
        resources = EmailClient.process_email_to_resources(email_item)
        all_raw_resources.extend(resources)

    print(f"fetch_emails_node parsed {len(all_raw_resources)} raw resources.")

    # Check if this was a manual trigger with a direct custom resource (for testing/direct insert)
    # The new_message content might contain a JSON string representing a manual resource entry.
    message_text = ""
    if isinstance(node_input, types.Content) and node_input.parts:
        message_text = node_input.parts[0].text or ""
    elif isinstance(node_input, str):
        message_text = node_input

    if message_text.startswith("MANUAL_RESOURCE:"):
        try:
            payload = json.loads(message_text.replace("MANUAL_RESOURCE:", ""))
            all_raw_resources.append(
                {
                    "title": payload.get("title", "Manual Entry"),
                    "url": payload.get("url", ""),
                    "source_email_subject": "Manual Dashboard Entry",
                    "source_email_sender": "User Dashboard",
                    "content": payload.get("content", ""),
                }
            )
            print("Added 1 manual resource from input payload.")
        except Exception as e:
            print(f"Failed to parse manual resource payload: {e}")

    # Also check if synthesis is explicitly requested in message
    synthesis_type = None
    if "synthesis:daily" in message_text.lower():
        synthesis_type = "daily"
    elif "synthesis:weekly" in message_text.lower():
        synthesis_type = "weekly"

    return make_event(
        message=f"Fetched {len(all_raw_resources)} raw resources.",
        state={
            "raw_resources": all_raw_resources,
            "synthesis_type": synthesis_type,
            "status": "fetched",
        },
    )


@node
def security_node(ctx: Context, node_input: Any) -> Event:
    """Node 2: Scrubs PII data (SSN, credit cards) and defends against prompt injection."""
    print("Executing security_node...")
    raw_resources = ctx.state.get("raw_resources", [])

    clean_resources = []
    redacted_count = 0
    injection_prevented_count = 0

    # SSN pattern
    ssn_pattern = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
    # Credit Card pattern
    cc_pattern = re.compile(r"\b(?:\d{4}[ -]?){3}\d{4}\b")

    # Common prompt-injection triggers
    injection_triggers = [
        "ignore previous instructions",
        "ignore all instructions",
        "bypass security check",
        "system prompt override",
        "always approve",
        "you must do",
        "bypass all rules",
        "forget everything",
    ]

    for r in raw_resources:
        content = r["content"]
        title = r["title"]

        # 1. PII Redaction
        has_ssn = bool(ssn_pattern.search(content))
        has_cc = bool(cc_pattern.search(content))

        if has_ssn or has_cc:
            content = ssn_pattern.sub("[REDACTED SSN]", content)
            content = cc_pattern.sub("[REDACTED CREDIT CARD]", content)
            redacted_count += 1
            print(f"PII scrubbed from resource: '{title}'")

        # 2. Prompt Injection Defense
        has_injection = any(
            trigger in content.lower() or trigger in title.lower()
            for trigger in injection_triggers
        )

        is_flagged = False
        if has_injection:
            injection_prevented_count += 1
            is_flagged = True
            content = (
                "[WARNING: Security Checkpoint Blocked Prompt Injection Attempt]\n\n"
                + content
            )
            print(f"Prompt injection attempt detected and neutralized in: '{title}'")

        clean_resources.append(
            {
                "title": title,
                "url": r["url"],
                "source_email_subject": r["source_email_subject"],
                "source_email_sender": r["source_email_sender"],
                "content": content,
                "is_flagged": is_flagged,
            }
        )

    print(
        f"security_node finished. Redacted: {redacted_count}, Injection blocked: {injection_prevented_count}"
    )
    return make_event(
        message=f"Security check completed. Redacted: {redacted_count}, Flagged: {redacted_count}",
        state={"clean_resources": clean_resources},
    )


@node
def ingest_node(ctx: Context, node_input: Any) -> Event:
    """Node 3: Computes embeddings, summaries, extracts tags, and performs vector similarity search to connect resources."""
    print("Executing ingest_node...")
    clean_resources = ctx.state.get("clean_resources", [])

    if not clean_resources:
        print("No new clean resources to ingest.")
        return make_event(
            message="No resources to ingest.", state={"ingested_count": 0}
        )

    try:
        client = get_genai_client()
    except Exception as e:
        print(f"Failed to initialize Gemini Client. Check API Key: {e}")
        # Return local fallback mock run
        for r in clean_resources:
            mock_emb = [0.1] * 768  # Mock 768-dim vector
            Database.insert_resource(
                title=r["title"],
                url=r["url"],
                email_subject=r["source_email_subject"],
                email_sender=r["source_email_sender"],
                content=r["content"],
                summary="Dry-run: Gemini client not available.",
                tags=["draft"],
                embedding=mock_emb,
            )
        return make_event(
            message="Ingested with local mock data (No API Key).",
            state={"ingested_count": len(clean_resources)},
        )

    ingested_count = 0
    for r in clean_resources:
        title = r["title"]
        content = r["content"]

        # Skip API calls for flagged inputs (save resources and prevent model exposure)
        if r.get("is_flagged"):
            Database.insert_resource(
                title=title,
                url=r["url"],
                email_subject=r["source_email_subject"],
                email_sender=r["source_email_sender"],
                content=content,
                summary="Security Blocked: Resource flagged for prompt injection.",
                tags=["security_flag"],
                embedding=[0.0] * 768,
            )
            ingested_count += 1
            continue

        try:
            # 1. Embed Content
            print(f"Generating embedding for: '{title}'...")
            emb_res = client.models.embed_content(
                model=Config.EMBEDDING_MODEL, contents=content
            )
            embedding = emb_res.embeddings[0].values

            # 2. Summarize Content
            print(f"Generating summary for: '{title}'...")
            summary_prompt = f"Summarize the main themes, key details, and actionable items of this document in 2-3 clear paragraphs:\n\n{content}"
            summary_res = client.models.generate_content(
                model=Config.GEMINI_MODEL, contents=summary_prompt
            )
            summary = summary_res.text.strip()

            # 3. Extract Tags
            print(f"Extracting tags for: '{title}'...")
            tags_prompt = f"Extract 3-5 keywords or topic tags from this text. Return them ONLY as a comma-separated list of lowercase words, no quotes, no numbers, no explanation:\n\nTitle: {title}\nText: {content[:1000]}"
            tags_res = client.models.generate_content(
                model=Config.GEMINI_MODEL, contents=tags_prompt
            )
            tags = [t.strip().lower() for t in tags_res.text.split(",") if t.strip()]

            # 4. Insert into SQLite Database
            resource_id = Database.insert_resource(
                title=title,
                url=r["url"],
                email_subject=r["source_email_subject"],
                email_sender=r["source_email_sender"],
                content=content,
                summary=summary,
                tags=tags,
                embedding=embedding,
            )
            ingested_count += 1

            # 5. Semantic Connection Search
            print(f"Finding semantic connections for: '{title}'...")
            similar = Database.find_similar_resources(embedding, top_k=2, threshold=0.5)

            # Filter out current article from results (should already not be matched because of transaction order, but let's make sure)
            similar = [s for s in similar if s["id"] != resource_id]

            for sim_res in similar:
                # Ask Gemini to write a connection description
                connection_prompt = (
                    f"Explain in 2 short sentences how these two articles relate to each other or reflect a pattern in the user's interests:\n\n"
                    f"Article A: {title}\nSummary A: {summary}\n\n"
                    f"Article B: {sim_res['title']}\nSummary B: {sim_res['summary']}\n\n"
                    f"Describe the connection:"
                )
                conn_desc_res = client.models.generate_content(
                    model=Config.GEMINI_MODEL, contents=connection_prompt
                )
                conn_desc = conn_desc_res.text.strip()

                # Save connection
                Database.insert_connection(
                    source_id=resource_id,
                    target_id=sim_res["id"],
                    description=conn_desc,
                )
                print(f"Saved connection between '{title}' and '{sim_res['title']}'")

        except Exception as ex:
            print(f"Error ingesting resource '{title}': {ex}")
            # Fallback local insert
            Database.insert_resource(
                title=title,
                url=r["url"],
                email_subject=r["source_email_subject"],
                email_sender=r["source_email_sender"],
                content=content,
                summary="Ingestion failed. Saved content only.",
                tags=["uncategorized"],
                embedding=[0.0] * 768,
            )
            ingested_count += 1

    return make_event(
        message=f"Successfully ingested {ingested_count} resources.",
        state={"ingested_count": ingested_count},
    )


@node
def synthesis_node(ctx: Context, node_input: Any) -> Event:
    """Node 4: Generates daily/weekly syntheses connecting user's interests and patterns."""
    print("Executing synthesis_node...")
    synthesis_type = ctx.state.get("synthesis_type")

    if not synthesis_type:
        # Default: check if daily/weekly is requested in state or triggered manually.
        # If not specified, do a light check if we have new resources and just finish.
        return make_event(
            message="Ingestion completed. Synthesis skipped (none requested).",
            state={"succeeded": True},
        )

    days = 1 if synthesis_type == "daily" else 7
    print(f"Generating {synthesis_type} synthesis over the last {days} days...")

    recent_resources = Database.get_recent_resources(days=days)
    if not recent_resources:
        msg = f"No new resources documented in the last {days} days to summarize."
        print(msg)
        Database.insert_summary(
            summary_type=synthesis_type,
            content=f"# {synthesis_type.capitalize()} Summary\n\nNo articles or notes were added during this period.",
        )
        return make_event(message=msg, state={"succeeded": True})

    try:
        client = get_genai_client()

        # Prepare list of items
        items_summary = []
        for idx, r in enumerate(recent_resources, 1):
            items_summary.append(
                f"{idx}. Title: {r['title']}\n"
                f"   URL: {r['url']}\n"
                f"   Tags: {', '.join(r['tags'])}\n"
                f"   Summary: {r['summary']}\n"
            )
        resources_text = "\n".join(items_summary)

        prompt = (
            f"You are the user's Second Brain Synthesis Agent.\n"
            f"Below is a list of articles, papers, and notes the user documented during the last {synthesis_type} cycle.\n"
            f"Please write a comprehensive, high-quality {synthesis_type} summary that:\n"
            f"1. Connects these topics together under main thematic pillars.\n"
            f"2. Highlights recurring patterns or evolving trends in the user's interests.\n"
            f"3. Recommends areas they might want to explore further or prioritize.\n\n"
            f"[Resources documented]\n{resources_text}\n\n"
            f"Format your response in beautiful Markdown, using bullet points, bold accents, and clear headers. Do not output HTML."
        )

        synthesis_res = client.models.generate_content(
            model=Config.GEMINI_MODEL, contents=prompt
        )
        synthesis_content = synthesis_res.text.strip()

        # Save synthesis
        Database.insert_summary(summary_type=synthesis_type, content=synthesis_content)
        print(f"Successfully saved {synthesis_type} summary!")
        return make_event(
            message=f"Successfully generated {synthesis_type} summary.",
            state={"succeeded": True, "synthesis_saved": True},
        )

    except Exception as e:
        print(f"Error generating synthesis: {e}")
        # Local fallback insert
        Database.insert_summary(
            summary_type=synthesis_type,
            content=f"# {synthesis_type.capitalize()} Summary (Fallback)\n\nCould not generate summary automatically: {e}",
        )
        return make_event(
            message=f"Ingestion completed, synthesis generated with local fallback due to: {e}",
            state={"succeeded": True},
        )


# Construct Workflow
root_agent = Workflow(
    name="second_brain_workflow",
    edges=[
        Edge(from_node=START, to_node=fetch_emails_node),
        Edge(from_node=fetch_emails_node, to_node=security_node),
        Edge(from_node=security_node, to_node=ingest_node),
        Edge(from_node=ingest_node, to_node=synthesis_node),
    ],
)


app = App(
    root_agent=root_agent,
    name="app",
)
