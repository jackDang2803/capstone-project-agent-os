# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import contextlib
import json
import os
from collections.abc import AsyncIterator

import google.auth
from a2a.server.tasks import InMemoryTaskStore
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.runners import Runner
from google.cloud import logging as google_cloud_logging
from google.genai import types
from pydantic import BaseModel

from app.app_utils import services
from app.app_utils.a2a import attach_a2a_routes
from app.app_utils.reasoning_engine_adapter import (
    attach_reasoning_engine_routes,
)
from app.app_utils.telemetry import (
    setup_agent_engine_telemetry,
    setup_telemetry,
)
from app.app_utils.typing import Feedback
from app.database import Database

load_dotenv()
setup_telemetry()
# Must run before get_fast_api_app to set the tracer provider resource.
setup_agent_engine_telemetry()

try:
    _, project_id = google.auth.default()
    logging_client = google_cloud_logging.Client()
    logger = logging_client.logger(__name__)
except Exception:
    project_id = None
    logger = None

allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Template directory setup
templates = Jinja2Templates(directory=os.path.join(AGENT_DIR, "app", "templates"))


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Runner for the A2A path, sharing the same session/artifact services as the
    # adk_api and reasoning_engine paths (see services.py). Imported here so the
    # agent is built after env/telemetry setup.
    from app.agent import app as adk_app
    from app.agent import root_agent

    runner = Runner(
        app=adk_app,
        session_service=services.get_session_service(),
        artifact_service=services.get_artifact_service(),
        auto_create_session=True,
    )
    # Shared by the A2A path and the reasoning_engine adapter routes.
    app.state.runner = runner
    app.state.agent_app_name = adk_app.name
    await attach_a2a_routes(
        app,
        agent=root_agent,
        runner=runner,
        task_store=InMemoryTaskStore(),
        rpc_path=f"/a2a/{adk_app.name}",
    )
    yield


app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    artifact_service_uri=services.ARTIFACT_SERVICE_URI,
    allow_origins=allow_origins,
    session_service_uri=services.SESSION_SERVICE_URI,
    otel_to_cloud=False,
    lifespan=lifespan,
)
app.title = "second-brain-agent"
app.description = "API for interacting with the Agent second-brain-agent"


@app.get("/")
def get_dashboard(request: Request):
    """Renders the Second Brain dark-mode Dashboard."""
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/resources")
def get_resources():
    """Returns all processed resources from the database."""
    try:
        resources = Database.get_all_resources()
        return {"success": True, "data": resources}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/connections")
def get_connections():
    """Returns all semantic pattern connections."""
    try:
        connections = Database.get_connections()
        return {"success": True, "data": connections}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/summaries")
def get_summaries(type: str | None = None):
    """Returns all generated daily/weekly reports."""
    try:
        summaries = Database.get_summaries(summary_type=type)
        return {"success": True, "data": summaries}
    except Exception as e:
        return {"success": False, "error": str(e)}


class TriggerPayload(BaseModel):
    synthesis_type: str | None = None


@app.post("/api/trigger")
def trigger_agent(payload: TriggerPayload):
    """Manually triggers email polling or synthesis workflow."""
    try:
        runner = app.state.runner

        # Build prompt message
        msg_text = "Ingest unread emails"
        if payload.synthesis_type:
            msg_text = f"synthesis:{payload.synthesis_type}"

        msg = types.Content(role="user", parts=[types.Part.from_text(text=msg_text)])

        # Run workflow
        events = list(
            runner.run(
                user_id="dashboard", session_id="session_dashboard", new_message=msg
            )
        )

        # Extract workflow final state count
        ingested_count = 0
        for ev in events:
            if ev.actions and ev.actions.state_delta:
                ingested_count = ev.actions.state_delta.get(
                    "ingested_count", ingested_count
                )

        return {"success": True, "ingested_count": ingested_count}
    except Exception as e:
        return {"success": False, "error": str(e)}


class ManualPayload(BaseModel):
    title: str
    url: str | None = ""
    content: str


@app.post("/api/manual")
def add_manual_entry(payload: ManualPayload):
    """Saves a direct manual note through the graph workflow."""
    try:
        runner = app.state.runner

        payload_dict = {
            "title": payload.title,
            "url": payload.url,
            "content": payload.content,
        }
        msg_text = f"MANUAL_RESOURCE:{json.dumps(payload_dict)}"
        msg = types.Content(role="user", parts=[types.Part.from_text(text=msg_text)])

        # Run workflow
        list(
            runner.run(
                user_id="dashboard", session_id="session_dashboard", new_message=msg
            )
        )

        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


# Proxy routes so the Vertex AI Console Playground (reasoning_engine SDK) can
# talk to this agent alongside the native adk_api routes.
attach_reasoning_engine_routes(app)


@app.post("/feedback")
def collect_feedback(feedback: Feedback) -> dict[str, str]:
    """Collect and log feedback.

    Args:
        feedback: The feedback data to log

    Returns:
        Success message
    """
    if logger:
        logger.log_struct(feedback.model_dump(), severity="INFO")
    return {"status": "success"}


# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
