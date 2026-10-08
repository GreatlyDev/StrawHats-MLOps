from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

DNSName = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
]
ImageReference = Annotated[
    str,
    StringConstraints(
        min_length=3, max_length=300, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/@-]*$"
    ),
]


class RegisterModel(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    version: str = Field(min_length=1, max_length=40)
    framework: str = Field(min_length=1, max_length=50)
    description: str = Field(default="", max_length=600)
    image: ImageReference


class PredictionInput(BaseModel):
    features: list[Annotated[float, Field(gt=0, le=30, allow_inf_nan=False)]] = Field(
        min_length=4, max_length=4
    )


class DeploymentInput(BaseModel):
    model_id: str = Field(min_length=1, max_length=100)
    name: DNSName
    namespace: DNSName = "strawhats"
    image: ImageReference
    replicas: int = Field(default=1, ge=1, le=10)
    cpu_millicores: int = Field(default=250, ge=50, le=8000)
    memory_mebibytes: int = Field(default=256, ge=128, le=16384)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class AssistantInput(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def last_message_is_user(self):
        if self.messages[-1].role != "user":
            raise ValueError("The final message must be from the user.")
        return self
