from dataclasses import dataclass, field
from typing import Any, dataclass_transform
from uuid import uuid4


def new_id() -> str:
    return str(uuid4())


@dataclass_transform(kw_only_default=True, frozen_default=True)
def entity_model(cls):
    return dataclass(cls, frozen=True, slots=True, kw_only=True)


@entity_model
class Entity:
    id: str = field(default_factory=new_id)
    type: str
    slug: str
    identifiers: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@entity_model
class Book(Entity):
    type: str = "book"
    title: str
    publication_date: str
    lang: str
    description: str
    author_ids: list[str] = field(default_factory=list)
    publisher_ids: list[str] = field(default_factory=list)


@entity_model
class Person(Entity):
    type: str = "person"
    name: str
    gender: str
    birth_date: str
    country: str
    occupations: list[str] = field(default_factory=list)
