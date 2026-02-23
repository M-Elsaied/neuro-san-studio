# KeyBank Financial Services Assistant

The **KeyBank Financial Services Assistant** is a hierarchically organized
multi-agent system that simulates KeyBank's financial services operations.
It covers consumer banking, commercial lending, wealth management, investment
advisory, estate and retirement planning, and risk management including fraud
prevention and compliance. Specialized agents coordinate through the AAOSA
pattern to deliver personalized, context-aware financial guidance.

The system currently operates in demo mode and can be grounded in real data and systems as needed.

---

## File

[keybank.hocon](../../../registries/industry/keybank.hocon)

---

## Description

This network organizes its intelligence into nested layers, with specialized
agents collaborating to resolve client inquiries across banking and financial
service domains. A central Client Relationship Manager (top-level "front-man"
agent) receives and parses incoming questions, dispatches relevant portions to
subject matter agents, and collates the final output for the end user while
maintaining clarity and helpfulness.

- **Domain Expertise Delegation**: Each agent is domain-specific and may
  offload subtasks to its own subordinate agents, mirroring how departments
  operate in an actual bank.
- **Human-like Dialogue Management**: The front-man agent ensures a smooth,
  conversational experience by abstracting the underlying agent collaboration
  from the user.
- **Interactive Follow-ups**: Agents may request additional user input before
  responding, using clearly formatted, human-friendly prompts.
- **Expandable Structure**: Adding new banking functions (e.g., tax advisory,
  digital banking) is as simple as plugging in new agents and updating tool
  references.

---

## Example Conversations

### Example 1

#### Human

```text
What mortgage options are available for a first-time home buyer?
```

#### AI (client_relationship_manager)

```text
Great question! Here are some mortgage options available for first-time
home buyers at KeyBank:

1. **Fixed-Rate Mortgages**: Offers a consistent interest rate and monthly payment over
   the life of the loan (typically 15 or 30 years), providing stability and predictability
   for budgeting.

2. **Adjustable-Rate Mortgages (ARMs)**: Start with a lower introductory interest rate
   that adjusts after a set period. This can be beneficial if you plan to sell or
   refinance before the rate adjusts.

3. **FHA Loans**: Backed by the Federal Housing Administration, these loans require a
   lower down payment (as low as 3.5%) and are designed for borrowers with moderate
   credit scores.

4. **Home Equity Lines of Credit (HELOC)**: While typically used by existing homeowners,
   understanding this option is helpful for future planning once you build equity in
   your home.

5. **Refinancing Options**: Once you have a mortgage, refinancing can help you secure a
   lower interest rate or adjust loan terms as your financial situation evolves.

Our mortgage advisors guide you through the entire application process, explaining terms,
rates, and repayment structures. Underwriting specialists then review your application
to ensure it meets KeyBank's lending criteria.

Would you like more details on any of these options or help getting started with an
application?
```

### Example 2

<!-- pyml disable-next-line MD024 -->
#### Human

```text
I need a business line of credit for my small company.
```

<!-- pyml disable-next-line MD024 -->
#### AI (client_relationship_manager)

```text
I can help you explore business lines of credit at KeyBank. Here's what you should know:

1. **Business Lines of Credit**: Provide flexible funding that you can draw on as needed
   to support day-to-day operations, manage cash flow, or fund growth initiatives.

2. **Eligibility**: Our business loan officers evaluate your business financials,
   creditworthiness, and market conditions to recommend appropriate products.

3. **Application Process**: We guide you through every step, ensuring all required
   documentation is complete and accurate. Our loan underwriters then analyze your
   financial statements, credit reports, and business plans to assess risk.

4. **Cash Management Solutions**: In addition to lines of credit, we offer payment
   processing, liquidity management, and treasury services tailored to your
   operational needs.

To get started, could you provide some additional details?
- What is the approximate credit amount you're looking for?
- What industry is your business in?
- How long has your company been in operation?

This will help me connect you with the right specialist for your needs.
```

---

## Architecture Overview

### Frontman Agent: `client_relationship_manager`

- Serves as the primary point of contact for all client inquiries
  at KeyBank.
- Understands client needs, provides personalized solutions, and
  ensures a seamless banking experience.
- Determines which specialized sub-agents to engage based on the inquiry.
- Coordinates and compiles responses from down-chain agents for a final resolution.

---

### Primary Domains (Tools called by the Frontman)

#### consumer_banking_specialist

- Handles individual client services including checking and
  savings accounts, personal loans, mortgages, credit cards,
  and financial wellness services.
- Delegates to:
    - `mortgage_advisor` - Provides mortgage options, refinancing,
      and home equity lines of credit
        - `underwriting_specialist` - Reviews and assesses
          mortgage applications against lending criteria
    - `personal_loan_officer` - Supports clients seeking personal
      loans with terms, eligibility, and repayment options
    - `credit_card_specialist` - Assists with choosing and
      managing credit card products, rewards, and dispute
      resolution

#### commercial_banking_specialist

- Manages financial services for business clients including
  loans, lines of credit, cash management, equipment financing,
  and commercial real estate services.
- Delegates to:
    - `business_loan_officer` - Assists businesses with securing
      loans and lines of credit
        - `loan_underwriter` - Analyzes financial documents and
          assesses risks for business loan applications
    - `cash_management_specialist` - Provides cash flow
      management solutions including payment processing and
      liquidity management
    - `equipment_financing_consultant` - Helps businesses secure
      financing for purchasing or leasing equipment

#### wealth_management_advisor

- Offers wealth management services including investment
  advisory, trust and estate planning, and retirement planning
  for high-net-worth clients.
- Delegates to:
    - `investment_decisioning_agent` - Analyzes client profiles,
      market data, and risk factors to recommend optimized
      investment strategies
        - `investment_portfolio_manager` - Manages investment
          portfolios, balancing risk and return to meet
          financial goals
    - `estate_planning_specialist` - Advises on estate planning
      strategies including wills, trusts, and tax implications
    - `retirement_plan_consultant` - Assists with retirement
      plans including IRAs, 401(k)s, and other retirement
      vehicles

#### risk_management_officer

- Provides risk management advice covering insurance, fraud
  prevention, and financial security for both individual and
  business clients.
- Delegates to:
    - `fraud_prevention_specialist` - Monitors transactions for
      signs of fraud and provides guidance on protecting
      financial information
    - `insurance_advisor` - Offers life, health, and property
      insurance products tailored to client needs
    - `compliance_officer` - Ensures KeyBank's operations adhere
      to legal regulations and internal policies

---

## Organizational Hierarchy

```text
client_relationship_manager
  ├─ consumer_banking_specialist
  │    ├─ mortgage_advisor
  │    │    └─ underwriting_specialist
  │    ├─ personal_loan_officer
  │    └─ credit_card_specialist
  ├─ commercial_banking_specialist
  │    ├─ business_loan_officer
  │    │    └─ loan_underwriter
  │    ├─ cash_management_specialist
  │    └─ equipment_financing_consultant
  ├─ wealth_management_advisor
  │    ├─ investment_decisioning_agent
  │    │    └─ investment_portfolio_manager
  │    ├─ estate_planning_specialist
  │    └─ retirement_plan_consultant
  └─ risk_management_officer
       ├─ fraud_prevention_specialist
       ├─ insurance_advisor
       └─ compliance_officer
```

---

## External Dependencies

None - this agent network operates using internal knowledge and organizational
structure simulation. It does not rely on external APIs, databases, or web
search services. All decision-making, coordination, and strategic planning
are handled through the internal agent hierarchy.

---
