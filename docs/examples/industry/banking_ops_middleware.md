# Banking Operations with Middleware

The **Banking Operations with Middleware** agent network demonstrates how to apply production-ready
LangChain middleware to a realistic multi-agent banking system. It builds on the
[Banking Operations](banking_ops.md) agent network by adding middleware layers that address
real-world production concerns.

This example is inspired by the blog post
[LangChain 1.0 Middleware: Steps to Building Production-Ready Agents](https://medium.com/@mohammed97ashraf/langchain-1-0-middleware-steps-to-building-production-ready-agents-f53a93aadc81),
which recommends starting simple and adding middleware incrementally.

---

## File

[banking_ops_middleware.hocon](../../../registries/industry/banking_ops_middleware.hocon)

---

## Description

The banking operations middleware network is a 15-agent hierarchical system covering account
servicing, fraud detection, credit underwriting, wealth management, compliance reporting, and
investment advisory. Four middleware layers are applied to specific agents, each addressing a
distinct production concern with a clear storyline.

---

## Middleware Layers

### 1. PII Redaction — "The Compliance Officer"

**Agent:** `customer_service_representative` (front man)

**Problem:** A customer asks about their account using a full credit card number or SSN. The agent
processes the request internally, but the response must never echo back sensitive numbers — this
is a PCI-DSS and privacy regulation requirement.

**Solution:** Two `PIIMiddleware` instances on the front man agent:
- One detects and redacts credit card numbers (16-digit patterns)
- One detects and redacts SSN patterns (XXX-XX-XXXX)

Both use `apply_to_output: true` so the agent can read the input but the response is sanitized.

**Reference:** [LangChain PIIMiddleware docs](https://docs.langchain.com/oss/python/langchain/middleware/built-in#pii-detection)

### 2. Summarization — "The Executive Summary"

**Agent:** `customer_service_representative` (front man)

**Problem:** The `compliance_report_generator` produces extremely detailed regulatory reports
(800+ words) with section numbers, citations, risk matrices, and appendices. Sending this
raw output to a customer would be overwhelming and unhelpful.

**Solution:** A `SummarizationMiddleware` on the front man condenses verbose conversation history
into concise summaries. It keeps only the last 2 messages intact and summarizes earlier ones
into a customer-friendly format of no more than 200 words. The front man's instructions also
enforce a 150-word bullet-point summary for compliance reports, keeping only overall status,
top findings, and action items with deadlines.

**Reference:** [LangChain SummarizationMiddleware docs](https://docs.langchain.com/oss/python/langchain/middleware/built-in#summarization)

### 3. Model Call Limit — "The Cost Controller"

**Agent:** `fraud_investigation_team`

**Problem:** Fraud investigations are open-ended by nature. The `fraud_investigation_team` calls
the `security_analyst`, who reasons through attack vectors, who may trigger further analysis.
Without guardrails, this recursive investigation could burn through 50+ LLM calls at significant
cost.

**Solution:** A `ModelCallLimitMiddleware` caps the investigation at 15 model calls per invocation.
When the limit is reached, the agent gracefully wraps up with whatever findings it has rather
than looping indefinitely.

**Reference:** [LangChain ModelCallLimitMiddleware docs](https://docs.langchain.com/oss/python/langchain/middleware/built-in#model-call-limit)

### 4. Human Approval — "The Wire Transfer Gate"

**Agent:** `account_manager`

**Problem:** Wire transfers — especially large or international ones — carry significant fraud
and compliance risk. A $50,000 international wire to Switzerland should not be processed
automatically without human oversight.

**Solution:** A `HumanApprovalMiddleware` on the `account_manager` intercepts outbound transfer
requests and requires explicit human (manager) approval before execution. This satisfies
regulatory requirements for dual-authorization on high-value payments.

**Reference:** [LangChain HumanApprovalMiddleware docs](https://docs.langchain.com/oss/python/langchain/middleware/built-in#human-approval)

---

## Prerequisites

This agent network requires the following setup:

### Python Dependencies

Nothing special beyond the base neuro-san-studio requirements.

### Environment Variables

Nothing special.

---

## Example Conversations

### PII Redaction

**Human:**
```text
Check the status of my account 4532-1234-5678-9012 and tell me the balance.
```

**AI:**
```text
I've looked up your account ending in [REDACTED_CREDIT_CARD]. Your current balance is $12,450.00.
The account is in good standing with no pending transactions.
```

Note how the credit card number from the input is redacted in the output by the PIIMiddleware.

### Compliance Report Summarization

**Human:**
```text
Generate a compliance report for my checking account for the last 30 days covering Dodd-Frank and AML regulations.
```

**AI:**
```text
Here's your compliance summary:

- **Overall Status**: Compliant with minor observations
- **Top Findings**: (1) 3 transactions flagged for enhanced due diligence review,
  (2) AML training completion at 94% — below 100% target
- **Action Items**: (1) Complete enhanced due diligence reviews by March 30,
  (2) Schedule remaining staff AML training by April 15
```

The compliance_report_generator produces an 800+ word detailed report. The front man's
instructions and SummarizationMiddleware condense it to a brief bullet-point summary.

### Fraud Investigation

**Human:**
```text
I noticed three unauthorized charges on my account from different countries - $2,500 from Nigeria,
$1,800 from Romania, and $950 from Indonesia, all within the last 48 hours.
```

The ModelCallLimitMiddleware ensures this complex multi-country fraud investigation completes
within 15 LLM calls rather than looping indefinitely between the fraud team and security analyst.

### Wire Transfer Approval

**Human:**
```text
I need to wire $50,000 from my checking account to an international account in Switzerland.
The beneficiary is Alpen Financial Group, IBAN CH93 0076 2011 6238 5295 7. Please process this today.
```

The HumanApprovalMiddleware on the account_manager requires manager authorization before the
wire transfer can be processed, adding a compliance gate for high-value international transactions.

---

## Architecture Overview

### Frontman Agent: customer_service_representative

- Main entry point for all customer inquiries
- Routes to specialized agents: account_manager, fraud_prevention_specialist, loan_officer, compliance_report_generator
- **Middleware:** PIIMiddleware (credit card + SSN redaction on output), SummarizationMiddleware (conversation history compression)

### Specialized Agents

- **account_manager** → relationship_manager, wealth_management_advisor, investment_specialist
  - **Middleware:** HumanApprovalMiddleware (wire transfer authorization gate)
- **fraud_prevention_specialist** → fraud_investigation_team, security_analyst
  - **fraud_investigation_team Middleware:** ModelCallLimitMiddleware (caps at 15 LLM calls)
- **loan_officer** → underwriter, mortgage_specialist, business_banking_officer
- **wealth_management_advisor** → investment_specialist, portfolio_manager
- **portfolio_manager** → trading_desk
- **compliance_report_generator** → generates detailed regulatory reports (800+ words)

---

## Debugging Hints

- **PII not being redacted:** Check that the regex pattern in the `detector` field matches your
  test data. The credit card pattern expects 16 digits with optional dashes or spaces.

- **Compliance reports too verbose:** The front man instructions enforce a 150-word summary.
  If the output is still long, check that the SummarizationMiddleware `keep` parameter is set
  to a low number (currently 2 messages).

- **Fraud investigation taking too long:** The ModelCallLimitMiddleware on `fraud_investigation_team`
  caps at 15 calls. If investigations still timeout, consider lowering `run_limit`.

- **Agent not responding:** This is a large network (15 agents). Ensure adequate timeout settings.

---

## Resources

- [Agent Network Hocon specification](https://github.com/cognizant-ai-lab/neuro-san/blob/main/docs/agent_hocon_reference.md#middleware)
  See how to define multiple middleware instances for your own agent networks.

- [AgentMiddleware Overview](https://docs.langchain.com/oss/python/langchain/middleware/overview)
  Good overview of AgentMiddleware.

- [Prebuilt Middleware](https://docs.langchain.com/oss/python/langchain/middleware/built-in)
  List of all built-in middleware classes available in LangChain.

- [LangChain 1.0 Middleware Blog](https://medium.com/@mohammed97ashraf/langchain-1-0-middleware-steps-to-building-production-ready-agents-f53a93aadc81)
  Step-by-step guide to building a production middleware stack.

- [LangChain Custom Middleware](https://www.linkedin.com/pulse/langchain-custom-middleware-agent-control-yash-sarode-yyhwf/)
  Patterns for custom middleware including dynamic model selection, role-based tool filtering, and retry logic.