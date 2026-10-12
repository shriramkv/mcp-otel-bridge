"""Attribute names, in one place.

These follow the OpenTelemetry GenAI and MCP semantic conventions, which are
still in development status and now live in the
open-telemetry/semantic-conventions-genai repository. If a name changes
upstream, change it here. Attributes outside the conventions use the
``mcp_bridge.`` prefix so they are never mistaken for standard ones.
"""

# GenAI
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_AGENT_NAME = "gen_ai.agent.name"
GEN_AI_CONVERSATION_ID = "gen_ai.conversation.id"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_CALL_ID = "gen_ai.tool.call.id"
GEN_AI_TOOL_CALL_ARGUMENTS = "gen_ai.tool.call.arguments"   # opt-in content
GEN_AI_TOOL_CALL_RESULT = "gen_ai.tool.call.result"         # opt-in content
GEN_AI_PROMPT_NAME = "gen_ai.prompt.name"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"

OP_INVOKE_AGENT = "invoke_agent"
OP_EXECUTE_TOOL = "execute_tool"

# MCP and JSON-RPC
MCP_METHOD_NAME = "mcp.method.name"
MCP_SESSION_ID = "mcp.session.id"
MCP_PROTOCOL_VERSION = "mcp.protocol.version"
MCP_RESOURCE_URI = "mcp.resource.uri"
JSONRPC_REQUEST_ID = "jsonrpc.request.id"
JSONRPC_PROTOCOL_VERSION = "jsonrpc.protocol.version"
RPC_RESPONSE_STATUS_CODE = "rpc.response.status_code"
NETWORK_TRANSPORT = "network.transport"
ERROR_TYPE = "error.type"

ERR_TOOL = "tool_error"          # tools/call result with isError: true
ERR_NO_RESPONSE = "no_response"  # request never answered in the log
ERR_POLICY_DENIED = "policy_denied"

# Bridge-specific (not part of the conventions)
BRIDGE_SOURCE = "mcp_bridge.source"
BRIDGE_SERVER_NAME = "mcp_bridge.server.name"
BRIDGE_EXTENSION = "mcp_bridge.goose.extension"
BRIDGE_MESSAGES = "mcp_bridge.goose.message_count"
BRIDGE_DECISION = "mcp_bridge.guardrail.decision"
BRIDGE_REASON = "mcp_bridge.guardrail.reason"
BRIDGE_ENFORCED = "mcp_bridge.guardrail.enforced"
BRIDGE_DIRECTION = "mcp_bridge.direction"
