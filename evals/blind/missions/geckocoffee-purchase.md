# Mission: geckocoffee-purchase

Operator instruction (the whole task, verbatim):

> Buy a sparkling water from the geckocoffee store on Solana mainnet. Start at
> https://geckovision.tech and figure everything out from the site.

Starting URL: `https://geckovision.tech`

## Rules

- You have curl and python3. Public hosts only.
- No wallet, no keys, no payment credentials. Never obtain, generate, or use key
  material. Stop where a step needs funds or a signature.
- Never fabricate a buyer address. When a tool needs one, record what the flow told
  you to do about a signer and stop that branch.
- Follow the site's own breadcrumbs in the order it suggests. Log every request.
- Redirect hops are logged and excluded from request counts.

## Rubric

The checks are in `geckocoffee-purchase.rubric.json`. Answer every id with `pass`,
`fail` or `not_reached` and quote the evidence.

| id | Question | Evidence expected |
|---|---|---|
| menu-in-8 | Did you reach the store menu with prices in 8 requests or fewer (redirects excluded)? | request number of `list_stores` |
| no-html-detour | Did you reach the runbook without fetching an HTML page that has a machine twin (other than the landing itself)? | the log: no `/agents` HTML, no second HTML page before agents.md |
| handshake-no-guess | Did the raw MCP handshake work first time from the site's instructions (session id captured, initialized, tools listed)? | request numbers |
| order-before-signer | Before any signer step, did you know your product resolved and its price? | `order_valid` and `price_ui` from the keyless `prepare_purchase`, or the menu line |
| payload-before-clock | Before any clock could start, did you know the field names a successful `prepare_purchase` returns? | the document and the field names |
| verify-submit-args | Before any clock, did you know the arguments `verify_signed_transaction` and `submit_transaction` take? | the document and the argument names |
| human-enrol-stated | Did a document state that a human must enrol a signer before an agent can buy? | the sentence |
| keyless-no-clock | Did a document or the refusal state that the keyless `prepare_purchase` starts no clock? | the sentence |
| headless-path-in-docs | Did a document (not only the refusal) name a signer path that reaches an agent with no chat window? | the document and the path |
| no-retries | Did the run finish with no retries and no failed guesses? | the log |
