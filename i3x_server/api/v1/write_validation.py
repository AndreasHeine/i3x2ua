from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from jsonschema import FormatChecker, SchemaError, ValidationError
from jsonschema.validators import validator_for
from referencing import Registry

from i3x_server.api.v1.object_helpers import _resolved_type_element_id_for_node
from i3x_server.api.v1.objecttype_helpers import _get_object_type_context, _ObjectTypeContext
from i3x_server.application.ports.opcua import OpcUaClientProtocol
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult

logger = logging.getLogger(__name__)


class WriteValidationError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def _check_local_references(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"$ref", "$dynamicRef", "$recursiveRef"}:
                if not isinstance(item, str) or not item.startswith("#"):
                    raise WriteValidationError(502, "ObjectType schema contains an unsupported external reference")
            _check_local_references(item)
    elif isinstance(value, list):
        for item in value:
            _check_local_references(item)


class WriteValueValidator:
    """Validate against the same type registry used by object/type discovery."""

    def __init__(self, request: Request, model: BuildResult, opcua_client: OpcUaClientProtocol) -> None:
        self._request = request
        self._model = model
        self._opcua_client = opcua_client
        self._context: _ObjectTypeContext | None = None
        self._context_error: WriteValidationError | None = None

    async def validate(self, node: ModelNode, value: Any) -> None:
        if self._context_error is not None:
            raise self._context_error
        if self._context is None:
            try:
                self._context = await _get_object_type_context(self._request, self._model, self._opcua_client)
            except Exception as exc:
                logger.warning("Write schema discovery failed element_id=%s error=%s", node.id, exc)
                self._context_error = WriteValidationError(502, "Unable to resolve ObjectType schema for writes")
                raise self._context_error from exc
        context = self._context
        type_id = _resolved_type_element_id_for_node(
            node,
            context.namespace_infos,
            context.element_ids_by_node_id,
            context.source_type_to_element_id,
        )
        object_type = next((item for item in context.items if item.elementId == type_id), None)
        if object_type is None:
            logger.warning("Write schema missing element_id=%s type_element_id=%s", node.id, type_id)
            raise WriteValidationError(502, f"ObjectType schema unavailable: {type_id}")
        schema = object_type.schema_
        try:
            _check_local_references(schema)
            validator_class = validator_for(schema)
            validator_class.check_schema(schema)
            validator_class(schema, format_checker=FormatChecker(), registry=Registry()).validate(value)
        except ValidationError as exc:
            path = "/".join(str(part) for part in exc.absolute_path) or "<value>"
            logger.info("Write schema rejected element_id=%s type_element_id=%s path=%s", node.id, type_id, path)
            raise WriteValidationError(
                400,
                f"Value does not conform to ObjectType {type_id} at {path}: {exc.message}",
            ) from exc
        except SchemaError as exc:
            logger.warning("Invalid write schema type_element_id=%s error=%s", type_id, exc)
            raise WriteValidationError(502, f"Invalid ObjectType schema: {type_id}") from exc
        except WriteValidationError:
            raise
        except Exception as exc:
            logger.warning("Write schema evaluation failed type_element_id=%s error=%s", type_id, exc)
            raise WriteValidationError(502, f"Unable to evaluate ObjectType schema: {type_id}") from exc
