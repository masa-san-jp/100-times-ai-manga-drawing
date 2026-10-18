from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Direction(Strict):
    marker: str = Field(min_length=1, max_length=100)
    text: str = Field(max_length=20000)


class Character(Strict):
    key: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    height_cm: float | None = Field(default=None, gt=0, le=500)
    images: list[str] = Field(min_length=1, max_length=1)


class Scene(Strict):
    key: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=20000)
    images: list[str] = Field(default_factory=list, max_length=7)


class PageInput(Strict):
    number: int = Field(gt=0)
    name: str
    instructions: str
    characters: list[str] = Field(default_factory=list, max_length=5)
    scene: str | None = None
    page_prompt: str | None = Field(default=None, max_length=20000)
    directions: list[Direction] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique(self):
        if len({x.marker for x in self.directions}) != len(self.directions):
            raise ValueError("演出番号が重複しています")
        if len(set(self.characters)) != len(self.characters):
            raise ValueError("人物が重複しています")
        return self


class Manifest(Strict):
    schema_version: Literal[1]
    title: str = Field(min_length=1, max_length=200)
    characters: list[Character] = Field(default_factory=list)
    scenes: list[Scene] = Field(default_factory=list)
    pages: list[PageInput] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique(self):
        for group in (self.characters, self.scenes):
            if len({x.key for x in group}) != len(group):
                raise ValueError("資料のkeyが重複しています")
        if len({x.number for x in self.pages}) != len(self.pages):
            raise ValueError("ページ番号が重複しています")
        return self


class Draft(Strict):
    name_hash: str
    instruction_hash: str
    characters: list[str] = Field(default_factory=list, max_length=5)
    scene: str | None = None
    page_prompt: str = Field(default="", max_length=20000)
    directions: list[Direction] = Field(default_factory=list)
    correction: str = Field(default="", max_length=20000)
    prompt_override: str | None = Field(default=None, max_length=20000)

    @model_validator(mode="after")
    def unique(self):
        if len({x.marker for x in self.directions}) != len(self.directions):
            raise ValueError("演出番号が重複しています")
        return self
