import json
from typing import Any, Dict, List, Optional, Type, Union

from langchain_core.exceptions import OutputParserException
from langchain_core.output_parsers import BaseOutputParser
from langchain_core.outputs import ChatGeneration, Generation
from langchain_core.pydantic_v1 import BaseModel, Field
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import FunctionDescription
from langchain_core.utils.json_schema import dereference_refs
from vertexai.generative_models import (  # type: ignore
    FunctionDeclaration,
)
from vertexai.generative_models import (
    Tool as VertexTool,
)


def _format_pydantic_to_vertex_function(
    pydantic_model: Type[BaseModel],
) -> FunctionDescription:
    schema = pydantic_model.schema()

    return {
        "name": schema["title"],
        "description": schema.get("description", ""),
        "parameters": _get_parameters_from_schema(schema=schema),
    }


def _format_tool_to_vertex_function(tool: BaseTool) -> FunctionDescription:
    "Format tool into the Vertex function API."
    if tool.args_schema:
        schema = tool.args_schema.schema()

        return {
            "name": tool.name or schema["title"],
            "description": tool.description or schema["description"],
            "parameters": _get_parameters_from_schema(schema=schema),
        }
    else:
        return {
            "name": tool.name,
            "description": tool.description,
            "parameters": {
                "properties": {
                    "__arg1": {"type": "string"},
                },
                "required": ["__arg1"],
                "type": "object",
            },
        }


def _format_tools_to_vertex_tool(
    tools: List[Union[BaseTool, Type[BaseModel], dict]],
) -> List[VertexTool]:
    "Format tool into the Vertex Tool instance."
    function_declarations = []
    for tool in tools:
        if isinstance(tool, BaseTool):
            func = _format_tool_to_vertex_function(tool)
        elif isinstance(tool, type) and issubclass(tool, BaseModel):
            func = _format_pydantic_to_vertex_function(tool)
        elif isinstance(tool, dict):
            func = {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": _get_parameters_from_schema(tool["parameters"]),
            }
        else:
            raise ValueError(f"Unsupported tool call type {tool}")
        function_declarations.append(FunctionDeclaration(**func))

    return [VertexTool(function_declarations=function_declarations)]


class ParametersSchema(BaseModel):
    """
    This is a schema of currently supported definitions in function calling.
    We need explicitly exclude `title` and `definitions` fields as they
    are not currently supported.

    All other fields will be passed through (as extra fields are allowed)
    and intercepted on `google.cloud.aiplatform` level
    """

    title: Optional[str] = Field(exclude=True)
    definitions: Optional[Any] = Field(exclude=True)
    items: Optional["ParametersSchema"]
    properties: Optional[Dict[str, "ParametersSchema"]]

    class Config:
        extra = "allow"


def _replace_all_ofs(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Assumes dereferenced schema."""
    new_schema: Dict[str, Any] = {}
    for k, v in schema.items():
        if isinstance(v, dict) and "allOf" in v:
            if isinstance(v["allOf"], list) and len(v["allOf"]) == 1:
                obj = dict(v["allOf"][0])
                obj["title"] = v.get("title", obj.get("title", ""))
                obj["description"] = " ".join(
                    (v.get("description", ""), obj.get("description", ""))
                )
                new_schema[k] = obj
            else:
                raise ValueError(
                    f"allOf expected to be a singleton list. Received {v['allOf']}"
                )
        elif isinstance(v, dict):
            new_schema[k] = _replace_all_ofs(v)
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            new_schema[k] = [_replace_all_ofs(s) for s in v]
        else:
            new_schema[k] = v
    return new_schema


def _get_parameters_from_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Given a schema, format the parameters key to match VertexAI
    expected input.

    Args:
        schema: Dictionary that must have the following keys.

    Returns:
        Dictionary with the formatted parameters.
    """

    schema = dereference_refs(schema)
    # TODO: Remove if vertexai api supports allOf elements in the future.
    schema = _replace_all_ofs(schema)
    model = ParametersSchema.parse_obj(schema)

    return model.dict(exclude_unset=True)


class PydanticFunctionsOutputParser(BaseOutputParser):
    """Parse an output as a pydantic object.

    This parser is used to parse the output of a ChatModel that uses
    Google Vertex function format to invoke functions.

    The parser extracts the function call invocation and matches
    them to the pydantic schema provided.

    An exception will be raised if the function call does not match
    the provided schema.

    Example:

        ... code-block:: python

            message = AIMessage(
                content="This is a test message",
                additional_kwargs={
                    "function_call": {
                        "name": "cookie",
                        "arguments": json.dumps({"name": "value", "age": 10}),
                    }
                },
            )
            chat_generation = ChatGeneration(message=message)

            class Cookie(BaseModel):
                name: str
                age: int

            class Dog(BaseModel):
                species: str

            # Full output
            parser = PydanticOutputFunctionsParser(
                pydantic_schema={"cookie": Cookie, "dog": Dog}
            )
            result = parser.parse_result([chat_generation])
    """

    pydantic_schema: Union[Type[BaseModel], Dict[str, Type[BaseModel]]]

    def parse_result(
        self, result: List[Generation], *, partial: bool = False
    ) -> BaseModel:
        if not isinstance(result[0], ChatGeneration):
            raise ValueError("This output parser only works on ChatGeneration output")
        message = result[0].message
        function_call = message.additional_kwargs.get("function_call", {})
        if function_call:
            function_name = function_call["name"]
            tool_input = function_call.get("arguments", {})
            if isinstance(self.pydantic_schema, dict):
                schema = self.pydantic_schema[function_name]
            else:
                schema = self.pydantic_schema
            return schema(**json.loads(tool_input))
        else:
            raise OutputParserException(f"Could not parse function call: {message}")

    def parse(self, text: str) -> BaseModel:
        raise ValueError("Can only parse messages")
