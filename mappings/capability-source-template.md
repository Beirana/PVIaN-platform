# Capability Source Mapping Template

> 不得根据名称、营销描述或 OpenAI-compatible 外形推断能力。

## Subject

- endpoint_id:
- endpoint_kind:
- provider_id:
- api_surface:
- api_version:
- model_requested:
- model_reported:
- model_revision:
- deployment_id:
- region:
- relevant_configuration_digest:
- observed_at:

## Claim

- capability_path:
- advertised:
- observed:
- effective:
- evidence_ids:
- unknowns:

## Evidence

- source_kind:
- capture_surface:
- media_type:
- redaction_profile:
- locator（不得含凭证或本机敏感绝对路径）:
- captured_at:
- retention_class:

## Review

- Reviewer:
- 配置/API/模型变化是否要求重新验证:
- 是否存在网关翻译:
- 是否错误地把运行器能力记为模型能力:
