"""db.py

Generic SQLite persistence layer for data-only models.

Design:
- entities: shared identity table
- one real table per registered model for scalar string fields
- one generic links table for entity-to-entity relationships
- one generic terms table for plain string lists

Naming conventions:
- types are singular:
    book, person
- tables are plural:
    books, people, entities, links, terms
- predicates are singular:
    author, publisher, occupation, identifier

Field mapping conventions:
- list fields ending in "_ids" are stored in links:
    author_ids -> predicate "author"
    publisher_ids -> predicate "publisher"
- other list[str] fields are stored in terms:
    occupations -> predicate "occupation"
    identifiers -> predicate "identifier"

You can override table names and predicate names explicitly in register().

This module does not validate your models.
Validation belongs in another module.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import fields
from pathlib import Path
from typing import Any, get_origin, get_type_hints

from models import Book, Entity, Person


SCHEMA_VERSION = 1


_BASE_DDL = """
CREATE TABLE IF NOT EXISTS entities (
    id         TEXT PRIMARY KEY,
    type       TEXT NOT NULL,
    slug       TEXT NOT NULL UNIQUE,
    extra      TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS links (
    id         INTEGER PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    predicate  TEXT NOT NULL,
    object_id  TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    position   INTEGER NOT NULL,
    UNIQUE(subject_id, predicate, position),
    UNIQUE(subject_id, predicate, object_id)
);

CREATE TABLE IF NOT EXISTS terms (
    id         INTEGER PRIMARY KEY,
    entity_id  TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    predicate  TEXT NOT NULL,
    value      TEXT NOT NULL,
    position   INTEGER NOT NULL,
    UNIQUE(entity_id, predicate, position),
    UNIQUE(entity_id, predicate, value)
);

CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);
CREATE INDEX IF NOT EXISTS idx_links_subject ON links(subject_id, predicate);
CREATE INDEX IF NOT EXISTS idx_links_object ON links(object_id, predicate);
CREATE INDEX IF NOT EXISTS idx_terms_entity ON terms(entity_id, predicate);
CREATE INDEX IF NOT EXISTS idx_terms_value ON terms(value, predicate);
"""


_IRREGULAR_PLURALS = {
    "entity": "entities",
    "person": "people",
}


def _q(identifier: str) -> str:
    """Quote SQL identifier."""
    return '"' + identifier.replace('"', '""') + '"'


def _qliteral(value: str) -> str:
    """Quote SQL string literal."""
    return "'" + value.replace("'", "''") + "'"


def _default_table(type_name: str) -> str:
    """
    Default table names are plural.

    book -> books
    person -> people
    entity -> entities
    """
    return _IRREGULAR_PLURALS.get(type_name, f"{type_name}s")


def _default_predicate(field_name: str) -> str:
    """
    Default predicate names are singular.

    author_ids -> author
    publisher_ids -> publisher
    identifiers -> identifier
    occupations -> occupation

    This is intentionally simple.
    Override in register() if a field name needs a special mapping.
    """
    if field_name.endswith("_ids"):
        return field_name[:-4] or field_name

    if field_name.endswith("s") and len(field_name) > 1:
        return field_name[:-1]

    return field_name


class Store:
    """
    Generic persistence store.

    Register model classes, call setup(), then use save/get/list/delete.
    """

    def __init__(self, path: str | Path = "data/app.sqlite3"):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(str(self.path), timeout=30.0)
        self._conn.row_factory = sqlite3.Row

        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")

        self._specs_by_cls: dict[type, dict[str, Any]] = {}
        self._specs_by_type: dict[str, dict[str, Any]] = {}
        self._setup_done = False

        # Generic fallback type.
        self.register(Entity)

    # ── lifecycle ────────────────────────────────────────────────────────

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False

    # ── registration ─────────────────────────────────────────────────────

    def register(
        self,
        cls: type[Entity],
        *,
        table: str | None = None,
        predicates: dict[str, str] | None = None,
    ) -> type[Entity]:
        """
        Register a model class.

        Optional overrides:
        - table: custom scalar table name
        - predicates: custom field -> predicate mapping

        Example:
            store.register(
                Book,
                table="books",
                predicates={
                    "author_ids": "author",
                    "publisher_ids": "publisher",
                    "identifiers": "identifier",
                },
            )
        """
        if cls in self._specs_by_cls:
            return cls

        type_name = getattr(cls, "__entity_type__", cls.__name__.lower())
        table_name = table or getattr(cls, "__table__", None) or _default_table(type_name)

        hints = get_type_hints(cls)
        model_fields: list[tuple[str, str, str]] = []
        scalar_fields: list[str] = []

        predicate_overrides = predicates or {}

        for field in fields(cls):
            name = field.name

            # Generic identity/common fields live in entities.
            if name in {"id", "type", "slug", "extra"}:
                continue

            hint = hints.get(name)

            # list[str] fields become either links or terms.
            if get_origin(hint) is list:
                if name.endswith("_ids"):
                    kind = "link"
                else:
                    kind = "term"

                predicate = predicate_overrides.get(name, _default_predicate(name))
                model_fields.append((name, kind, predicate))

            # Simple scalar text columns.
            elif hint is str:
                model_fields.append((name, "scalar", name))
                scalar_fields.append(name)

            # Other types are ignored by this persistence layer.
            # Extend only if/when needed.

        spec = {
            "cls": cls,
            "type": type_name,
            "table": table_name,
            "fields": model_fields,
            "scalar_fields": scalar_fields,
        }

        self._specs_by_cls[cls] = spec
        self._specs_by_type[type_name] = spec

        if self._setup_done:
            with self._conn:
                self._ensure_table(spec)

        return cls

    def setup(self) -> None:
        """Create base tables and all registered model tables."""
        current = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema version {current} is newer than this code "
                f"supports ({SCHEMA_VERSION})."
            )

        if self._setup_done:
            return

        with self._conn:
            self._conn.executescript(_BASE_DDL)

            for spec in self._specs_by_cls.values():
                self._ensure_table(spec)

            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

        self._setup_done = True

    # ── generic actions ──────────────────────────────────────────────────

    def save(self, model: Entity) -> Entity:
        """Create or update an entity."""
        spec = self._spec_for(model)

        with self._conn:
            self._save_entity(model, spec)
            self._save_scalar(model, spec)
            self._save_lists(model, spec)

        return model

    def get(self, entity_id: str) -> Entity:
        """Load one entity by id."""
        row = self._conn.execute(
            "SELECT * FROM entities WHERE id = ?",
            (entity_id,),
        ).fetchone()

        if row is None:
            raise KeyError(entity_id)

        spec = self._specs_by_type.get(row["type"])
        if spec is None:
            spec = self._specs_by_type.get("entity")

        if spec is None:
            raise LookupError(f"Unknown entity type: {row['type']!r}")

        kwargs: dict[str, Any] = {
            "id": row["id"],
            "type": row["type"],
            "slug": row["slug"],
            "extra": json.loads(row["extra"] or "{}"),
        }

        if spec["scalar_fields"]:
            scalar_row = self._conn.execute(
                f"SELECT * FROM {_q(spec['table'])} WHERE id = ?",
                (entity_id,),
            ).fetchone()

            for column in spec["scalar_fields"]:
                value = scalar_row[column] if scalar_row is not None else ""
                kwargs[column] = "" if value is None else value

        for name, kind, predicate in spec["fields"]:
            if kind == "link":
                rows = self._conn.execute(
                    """
                    SELECT object_id
                    FROM links
                    WHERE subject_id = ? AND predicate = ?
                    ORDER BY position
                    """,
                    (entity_id, predicate),
                ).fetchall()

                kwargs[name] = [r["object_id"] for r in rows]

            elif kind == "term":
                rows = self._conn.execute(
                    """
                    SELECT value
                    FROM terms
                    WHERE entity_id = ? AND predicate = ?
                    ORDER BY position
                    """,
                    (entity_id, predicate),
                ).fetchall()

                kwargs[name] = [r["value"] for r in rows]

        return spec["cls"](**kwargs)

    def list_entities(
        self,
        entity_type: str | None = None,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Entity]:
        """List entities by type."""
        sql = "SELECT id FROM entities"
        params: list[Any] = []

        if entity_type is not None:
            sql += " WHERE type = ?"
            params.append(entity_type)

        sql += " ORDER BY slug LIMIT ? OFFSET ?"
        params.extend([limit if limit is not None else -1, offset])

        rows = self._conn.execute(sql, params).fetchall()
        return [self.get(row["id"]) for row in rows]

    def delete(self, entity_id: str) -> bool:
        """Delete one entity. Returns True if something was deleted."""
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM entities WHERE id = ?",
                (entity_id,),
            )
        return cur.rowcount > 0

    # ── internals ────────────────────────────────────────────────────────

    def _spec_for(self, model: Entity) -> dict[str, Any]:
        spec = self._specs_by_cls.get(type(model))

        if spec is None:
            spec = self._specs_by_type.get(model.type)

        if spec is None:
            raise LookupError(
                f"No registered model for {type(model).__name__} / "
                f"type={getattr(model, 'type', None)!r}"
            )

        return spec

    def _ensure_table(self, spec: dict[str, Any]) -> None:
        if not spec["scalar_fields"]:
            return

        table = _q(spec["table"])

        columns_sql = ", ".join(
            f"{_q(column)} TEXT NOT NULL DEFAULT ''"
            for column in spec["scalar_fields"]
        )

        self._conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table} (
                id TEXT PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
                {columns_sql}
            )
            """
        )

        existing = {
            row["name"]
            for row in self._conn.execute(
                f"PRAGMA table_info({_qliteral(spec['table'])})"
            )
        }

        # Light migration support:
        # if you add a new scalar field later, add the column.
        for column in spec["scalar_fields"]:
            if column not in existing:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {_q(column)} TEXT NOT NULL DEFAULT ''"
                )

    def _save_entity(self, model: Entity, spec: dict[str, Any]) -> None:
        existing = self._conn.execute(
            "SELECT type FROM entities WHERE id = ?",
            (model.id,),
        ).fetchone()

        # This is a tiny integrity guard, not business validation.
        # It prevents one id from silently changing to another type.
        if existing and existing["type"] != spec["type"]:
            raise ValueError(
                f"Entity {model.id!r} already has type {existing['type']!r}, "
                f"cannot save as {spec['type']!r}"
            )

        extra = json.dumps(
            model.extra or {},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

        self._conn.execute(
            """
            INSERT INTO entities (id, type, slug, extra)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                slug = excluded.slug,
                extra = excluded.extra,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            (model.id, spec["type"], model.slug, extra),
        )

    def _save_scalar(self, model: Entity, spec: dict[str, Any]) -> None:
        if not spec["scalar_fields"]:
            return

        columns = ["id", *spec["scalar_fields"]]
        values: list[Any] = [model.id]

        for column in spec["scalar_fields"]:
            value = getattr(model, column, "")
            values.append("" if value is None else value)

        insert_columns = ", ".join(_q(column) for column in columns)
        placeholders = ", ".join("?" for _ in columns)

        updates = ", ".join(
            f"{_q(column)} = excluded.{_q(column)}"
            for column in spec["scalar_fields"]
        )

        sql = f"""
            INSERT INTO {_q(spec['table'])} ({insert_columns})
            VALUES ({placeholders})
            ON CONFLICT(id) DO UPDATE SET
                {updates}
        """

        self._conn.execute(sql, values)

    def _save_lists(self, model: Entity, spec: dict[str, Any]) -> None:
        for name, kind, predicate in spec["fields"]:
            if kind == "scalar":
                continue

            values = getattr(model, name, []) or []

            if kind == "link":
                table = "links"
                subject_col = "subject_id"
                value_col = "object_id"
            else:
                table = "terms"
                subject_col = "entity_id"
                value_col = "value"

            self._conn.execute(
                f"DELETE FROM {table} WHERE {subject_col} = ? AND predicate = ?",
                (model.id, predicate),
            )

            self._conn.executemany(
                f"""
                INSERT INTO {table} ({subject_col}, predicate, {value_col}, position)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (model.id, predicate, value, position)
                    for position, value in enumerate(values)
                ],
            )


# ── optional one-file bootstrap for current models ────────────────────────

def create_store(path: str | Path = "data/library.sqlite3") -> Store:
    """
    Simple bootstrap for the current project.

    Defaults already map:
    - Book.author_ids -> predicate "author"
    - Book.publisher_ids -> predicate "publisher"
    - Person.occupations -> predicate "occupation"
    - Entity.identifiers -> predicate "identifier"

    and tables:
    - Book -> books
    - Person -> people

    If you want to be fully explicit, use:

        store.register(
            Book,
            table="books",
            predicates={
                "author_ids": "author",
                "publisher_ids": "publisher",
                "identifiers": "identifier",
            },
        )

        store.register(
            Person,
            table="people",
            predicates={
                "occupations": "occupation",
                "identifiers": "identifier",
            },
        )
    """
    store = Store(path)
    store.register(Book)
    store.register(Person)
    store.setup()
    return store
