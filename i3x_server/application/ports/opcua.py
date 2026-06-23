"""Application-facing OPC UA port surface.

The domain module remains the single source of truth. This facade exists so
presentation code can depend on application ports instead of domain ports.
"""

from i3x_server.domain.ports.opcua import (
    OpcUaClientProtocol,
    OpcUaConnectionSnapshot,
    OpcUaNamespaceInfo,
    OpcUaNodeInfo,
    OpcUaObjectTypeInfo,
    OpcUaObjectTypeMemberInfo,
    OpcUaOperationalLimits,
    OpcUaReferenceInfo,
    OpcUaRequestMetrics,
    OpcUaRuntimeMetrics,
    OpcUaSubscriptionCapabilities,
)

__all__ = [
    "OpcUaClientProtocol",
    "OpcUaConnectionSnapshot",
    "OpcUaNamespaceInfo",
    "OpcUaNodeInfo",
    "OpcUaObjectTypeInfo",
    "OpcUaObjectTypeMemberInfo",
    "OpcUaOperationalLimits",
    "OpcUaReferenceInfo",
    "OpcUaRequestMetrics",
    "OpcUaRuntimeMetrics",
    "OpcUaSubscriptionCapabilities",
]
