# Track A site adapters

Source references checked on 2026-11-07. This review checked public interfaces
and published links. Production terms, robots policies and rates still require
human verification. The checked-in registry keeps all entries unverified.

| Adapter | Public interface and fields | Verification |
| --- | --- | --- |
| SEC EDGAR | Company directory `/files/company_tickers.json`, `data.sec.gov/submissions/CIK<10-digit CIK>.json`, parallel `accessionNumber`, `filingDate`, `form`, `primaryDocument` and historical file descriptors, archive paths by CIK and accession. | [SEC developer documentation](https://www.sec.gov/search-filings/edgar-application-programming-interfaces), [EDGAR archive and directory documentation](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data). |
| SEC full-text | The optional query uses `efts.sec.gov/LATEST/search-index`, with `q`, `ciks`, `forms`, `dateRange`, `startdt` and `enddt`. The adapter expects hit IDs carrying accession and filename, with filer and filing metadata. | [SEC search UI and filters](https://www.sec.gov/edgar/search/efts-faq.html). The raw EFTS response was unavailable during reference review. The wire contract was tested with a synthetic response. |
| Delaware Code | Published title, chapter and subchapter HTML with section navigation. The checked-in Title 8, Chapter 1 page list contains the public subchapter paths. Other chapters can follow published navigation or accept an explicit chapter URL or subchapter. | [Directors and officers HTML](https://delcode.delaware.gov/title8/c001/sc04/index.html), [mergers and conversions HTML](https://delcode.delaware.gov/title8/c001/sc09/index.html). |
| Delaware Legislature | `/BillDetail/<numeric ID>` identifies the General Assembly. The adapter follows the published original, engrossed or session-law HTML link and preserves its exact query. | [BillDetail example and version links](https://legis.delaware.gov/BillDetail/142297). |
| Delaware Courts | Public Opinions and Orders listing with the `Search for...` field and a results table containing caption, date, docket and court. Opinions use published `/opinions/download.aspx?id=…` URLs. | [Opinions and Orders](https://courts.delaware.gov/opinions/). The live form submission method was not verified. The Chromium playbook was verified with synthetic GET and POST forms and refuses POST. |
| CourtListener | REST v4 search (`q`, `type=o`, optional `court`), opinion detail, and `POST /api/rest/v4/citation-lookup/` with form field `text`. Lookup compares the citation, case name and exact decision date. | [Search](https://www.courtlistener.com/help/api/rest/v4/search/), [case law](https://wiki.free.law/c/courtlistener/help/api/rest/v4/case-law), [citation lookup](https://www.courtlistener.com/help/api/rest/v4/citation-lookup/). |

Adapters accept an `AdapterTask(kind, params)` and a run-scoped `AdapterContext`.
The context submits every acquisition to Fetcher. The registry resolves official
methods before scripted methods and an optional Operator. A failed Fetcher
policy prevents downgrade to another method.

## Playbook review and canaries

Versioned YAML files use `playbooks/<site>/<task>@<version>.yaml`. A repair can
propose a different locator for one step after the original action and
expectation succeed under Action Guard. The database records a pending diff and
screenshot hash. A reviewer approves a specific proposal with
`gba-agent playbooks approve <id> --reviewer <name>`. Approval publishes a new
immutable version.

Run `gba-agent canary` from the workspace with the test dependencies and
Chromium installed. It exercises all five adapters with synthetic source
responses, including real Chromium for the court playbook.

Live smoke probes are disabled by default and in CI. To opt in outside CI, use
`gba-agent canary --live` and supply JSON parameters for the desired public
document probes through `GBA_LIVE_SEC_EDGAR_PARAMS`, `GBA_LIVE_DE_CODE_ONLINE_PARAMS`,
`GBA_LIVE_DE_LEGISLATURE_PARAMS`, `GBA_LIVE_DE_COURTS_PARAMS` or
`GBA_LIVE_COURTLISTENER_PARAMS`. These probes use GET through Fetcher and
respect the configured registry and terms policy.

