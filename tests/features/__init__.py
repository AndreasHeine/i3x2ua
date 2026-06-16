"""
Feature-aligned test packages.

Reorganizes the monolithic test_api.py into focused feature test modules:
  - app/ → app bootstrap, landing page, static assets
  - ua/ → OPC UA connection and diagnostics
  - v1_info/ → /v1/info endpoint
  - v1_namespaces/ → /v1/namespaces
  - v1_objecttypes/ → /v1/objecttypes endpoints
  - v1_relationshiptypes/ → /v1/relationshiptypes
  - v1_objects/ → /v1/objects queries
  - v1_values/ → /v1/*/value endpoints
  - v1_history/ → /v1/*/history endpoints
  - subscriptions/ → /v1/subscriptions lifecycle and updates
  - mcp/ → MCP JSON-RPC, tools, prompts, resources, roots
  - conftest/ → Shared fixtures and utilities
"""
