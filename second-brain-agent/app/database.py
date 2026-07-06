# app/database.py
import datetime
import json
import uuid
from typing import Any

import libsql_client

from app.config import Config


class Database:
    _client = None

    @classmethod
    def get_client(cls):
        if cls._client is None:
            url = Config.TURSO_DATABASE_URL
            auth_token = Config.TURSO_AUTH_TOKEN

            if url:
                # Connect to Turso Cloud SQLite
                cls._client = libsql_client.create_client_sync(
                    url=url, auth_token=auth_token
                )
            else:
                # Fallback to local SQLite file
                # libsql_client requires 'file:' prefix for local paths
                db_url = f"file:{Config.LOCAL_DB_PATH}"
                cls._client = libsql_client.create_client_sync(url=db_url)
        return cls._client

    @classmethod
    def init_db(cls):
        """Initializes tables in SQLite/Turso database."""
        client = cls.get_client()

        # Resources table
        client.execute("""
            CREATE TABLE IF NOT EXISTS resources (
                id TEXT PRIMARY KEY,
                title TEXT,
                url TEXT,
                source_email_subject TEXT,
                source_email_sender TEXT,
                content TEXT,
                summary TEXT,
                tags TEXT,
                created_at TEXT,
                embedding TEXT
            )
        """)

        # Connections table
        client.execute("""
            CREATE TABLE IF NOT EXISTS connections (
                id TEXT PRIMARY KEY,
                source_id TEXT,
                target_id TEXT,
                description TEXT,
                created_at TEXT,
                FOREIGN KEY (source_id) REFERENCES resources (id),
                FOREIGN KEY (target_id) REFERENCES resources (id)
            )
        """)

        # Summaries table
        client.execute("""
            CREATE TABLE IF NOT EXISTS summaries (
                id TEXT PRIMARY KEY,
                type TEXT,
                content TEXT,
                created_at TEXT
            )
        """)

    @classmethod
    def insert_resource(
        cls,
        title: str,
        url: str,
        email_subject: str,
        email_sender: str,
        content: str,
        summary: str,
        tags: list[str],
        embedding: list[float],
    ) -> str:
        """Inserts a new resource and returns its ID."""
        client = cls.get_client()
        resource_id = str(uuid.uuid4())
        created_at = datetime.datetime.utcnow().isoformat()

        # Serialize fields
        tags_str = json.dumps(tags)
        embedding_str = json.dumps(embedding)

        client.execute(
            "INSERT INTO resources (id, title, url, source_email_subject, source_email_sender, content, summary, tags, created_at, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                resource_id,
                title,
                url,
                email_subject,
                email_sender,
                content,
                summary,
                tags_str,
                created_at,
                embedding_str,
            ],
        )
        return resource_id

    @classmethod
    def get_all_resources(cls) -> list[dict[str, Any]]:
        client = cls.get_client()
        rs = client.execute("SELECT * FROM resources ORDER BY created_at DESC")

        resources = []
        for row in rs.rows:
            resources.append(
                {
                    "id": row[0],
                    "title": row[1],
                    "url": row[2],
                    "source_email_subject": row[3],
                    "source_email_sender": row[4],
                    "content": row[5],
                    "summary": row[6],
                    "tags": json.loads(row[7]) if row[7] else [],
                    "created_at": row[8],
                    "embedding": json.loads(row[9]) if row[9] else [],
                }
            )
        return resources

    @classmethod
    def get_recent_resources(cls, days: int = 7) -> list[dict[str, Any]]:
        client = cls.get_client()
        cutoff_date = (
            datetime.datetime.utcnow() - datetime.timedelta(days=days)
        ).isoformat()

        rs = client.execute(
            "SELECT * FROM resources WHERE created_at >= ? ORDER BY created_at DESC",
            [cutoff_date],
        )

        resources = []
        for row in rs.rows:
            resources.append(
                {
                    "id": row[0],
                    "title": row[1],
                    "url": row[2],
                    "source_email_subject": row[3],
                    "source_email_sender": row[4],
                    "content": row[5],
                    "summary": row[6],
                    "tags": json.loads(row[7]) if row[7] else [],
                    "created_at": row[8],
                    "embedding": json.loads(row[9]) if row[9] else [],
                }
            )
        return resources

    @classmethod
    def insert_connection(cls, source_id: str, target_id: str, description: str):
        client = cls.get_client()
        conn_id = str(uuid.uuid4())
        created_at = datetime.datetime.utcnow().isoformat()
        client.execute(
            "INSERT INTO connections (id, source_id, target_id, description, created_at) VALUES (?, ?, ?, ?, ?)",
            [conn_id, source_id, target_id, description, created_at],
        )

    @classmethod
    def get_connections(cls) -> list[dict[str, Any]]:
        client = cls.get_client()
        rs = client.execute("""
            SELECT c.id, c.source_id, c.target_id, c.description, c.created_at,
                   r1.title as source_title, r2.title as target_title
            FROM connections c
            JOIN resources r1 ON c.source_id = r1.id
            JOIN resources r2 ON c.target_id = r2.id
            ORDER BY c.created_at DESC
        """)
        connections = []
        for row in rs.rows:
            connections.append(
                {
                    "id": row[0],
                    "source_id": row[1],
                    "target_id": row[2],
                    "description": row[3],
                    "created_at": row[4],
                    "source_title": row[5],
                    "target_title": row[6],
                }
            )
        return connections

    @classmethod
    def insert_summary(cls, summary_type: str, content: str):
        client = cls.get_client()
        summary_id = str(uuid.uuid4())
        created_at = datetime.datetime.utcnow().isoformat()
        client.execute(
            "INSERT INTO summaries (id, type, content, created_at) VALUES (?, ?, ?, ?)",
            [summary_id, summary_type, content, created_at],
        )

    @classmethod
    def get_summaries(cls, summary_type: str | None = None) -> list[dict[str, Any]]:
        client = cls.get_client()
        if summary_type:
            rs = client.execute(
                "SELECT * FROM summaries WHERE type = ? ORDER BY created_at DESC",
                [summary_type],
            )
        else:
            rs = client.execute("SELECT * FROM summaries ORDER BY created_at DESC")

        summaries = []
        for row in rs.rows:
            summaries.append(
                {"id": row[0], "type": row[1], "content": row[2], "created_at": row[3]}
            )
        return summaries

    @classmethod
    def find_similar_resources(
        cls, query_embedding: list[float], top_k: int = 3, threshold: float = 0.4
    ) -> list[dict[str, Any]]:
        """Calculates cosine similarity in Python against all stored resources."""
        if not query_embedding:
            return []

        resources = cls.get_all_resources()
        similar_resources = []

        for r in resources:
            db_emb = r.get("embedding")
            if not db_emb or len(db_emb) != len(query_embedding):
                continue

            # Cosine similarity (since Gemini embeddings are normalized, dot product is cosine similarity)
            sim = sum(x * y for x, y in zip(query_embedding, db_emb, strict=True))

            if sim >= threshold:
                similar_resources.append((sim, r))

        # Sort by similarity descending
        similar_resources.sort(key=lambda x: x[0], reverse=True)

        # Return top_k resources with similarity score attached
        results = []
        for sim, r in similar_resources[:top_k]:
            r_copy = r.copy()
            r_copy["similarity"] = sim
            # Embeddings are large, remove from results to save bandwidth
            r_copy.pop("embedding", None)
            results.append(r_copy)

        return results
