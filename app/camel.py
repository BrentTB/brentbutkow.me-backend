from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


# snake_case fields in Python, camelCase JSON on the wire (FastAPI serializes by alias).
class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)
