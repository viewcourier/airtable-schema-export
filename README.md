# airtable-schema-export

A zero-dependency Python command line tool that exports the schema of an Airtable base (tables, fields, field types, select choices, linked tables, formulas, views, primary fields and descriptions) to JSON, a Markdown data dictionary and a CSV with one row per field. One file, `airtable_schema_export.py`, Python 3.9 or newer, standard library only, nothing to install.

```
export AIRTABLE_TOKEN=your_personal_access_token
python3 airtable_schema_export.py --base appXXXXXXXXXXXXXX
```

## Why

Airtable has no built-in way to export or print the structure of a base. Its CSV export covers the records of one view, not the tables, field types, select options or links between tables. If you want a data dictionary for documentation, an audit, a migration or a review, you either click through every table or call the metadata API. This tool does the second for you and writes the result as files you can commit, diff or paste into a wiki.

## What you get

For `--base appXXXXXXXXXXXXXX` the tool writes, into `--out` (default: the current directory):

- `<base-name>-schema.json`: the raw response of Airtable's metadata API plus a `generated_at` timestamp and the base ID and name.
- `<base-name>-schema.md`: a data dictionary with one `##` section per table: primary field, a table of `Field | Type | Details | Description`, and the list of views.
- `<base-name>-schema.csv`: one row per field with the columns `base,table,field,field_id,type,details,description,is_primary`.

`<base-name>` is the base name in lowercase with dashes (`Client CRM` becomes `client-crm`). If the token cannot list bases, the base ID is used instead.

The `Details` column is derived from the field options:

| Field type | Details |
| --- | --- |
| single and multiple select | the choice names |
| linked record | the name of the linked table (resolved from its ID), the inverse field, single-record preference |
| formula, rollup, lookup, count | the formula text, the fields it references, the link field it goes through, the result type |
| number, currency, percent, rating, duration | precision, currency symbol, maximum, icon, format |
| date, date and time, created / last modified time | date format, time format, time zone |
| anything else | the remaining options as `key: value` |

## Install

```
git clone https://github.com/viewcourier/airtable-schema-export.git
cd airtable-schema-export
python3 airtable_schema_export.py --help
```

Or just download `airtable_schema_export.py`; it has no dependencies.

## Usage

```
python3 airtable_schema_export.py --base BASE_ID [--format json|md|csv|all] [--out DIR] [--token TOKEN]
python3 airtable_schema_export.py --list-bases
```

| Option | Meaning |
| --- | --- |
| `--base <id>` | Base ID, starts with `app`. Required unless you use `--list-bases`. |
| `--format` | `json`, `md`, `csv` or `all`. Default `all`. |
| `--out <dir>` | Output directory. Default: current directory. Created if missing. |
| `--list-bases` | Print the bases the token can access (`id`, name, permission level, tab separated) and exit. |
| `--token <token>` | Personal access token. Prefer the `AIRTABLE_TOKEN` environment variable, because command line arguments can be seen by other users of the machine. |

The token is only sent to `api.airtable.com`, and is never written to disk or printed.

Exit codes: `0` success, `1` error (bad token, no access, unknown base, network, cannot write), `2` bad arguments or no token.

### Examples

```
# find the base ID
python3 airtable_schema_export.py --list-bases

# everything, into ./docs
python3 airtable_schema_export.py --base appXXXXXXXXXXXXXX --out docs

# only the Markdown data dictionary
python3 airtable_schema_export.py --base appXXXXXXXXXXXXXX --format md
```

## Token scopes

Create a personal access token at [airtable.com/create/tokens](https://airtable.com/create/tokens) with the scope `schema.bases:read` and access to the base (or to all bases in the workspace). That scope is used both for `--list-bases` and for reading the schema. No record data is read, so `data.records:read` is not needed.

## Example Markdown output

```markdown
# Client CRM: data dictionary

Base ID: `appXXXXXXXXXXXXXX`. Tables: 2. Generated: 2026-01-15T09:30:00Z.

## Clients

Our customers

Table ID: `tblAAAAAAAAAAAAAA`. Primary field: **Name**.

| Field | Type | Details | Description |
| --- | --- | --- | --- |
| Name | singleLineText |  | Legal name of the client |
| Status | singleSelect | choices: Lead, Active, Churned |  |
| Monthly fee | currency | precision: 2; symbol: € |  |
| Orders | multipleRecordLinks | links to: Orders; inverse field: Orders.Client |  |
| Label | formula | formula: {Name} & " (" & {Status} & ")"; references: Name, Status; result: singleLineText |  |

Views:

- All clients (grid)
- Pipeline (kanban)

## Orders

Table ID: `tblBBBBBBBBBBBBBB`. Primary field: **Reference**.

| Field | Type | Details | Description |
| --- | --- | --- | --- |
| Reference | singleLineText |  |  |
| Client | multipleRecordLinks | links to: Clients; inverse field: Clients.Orders |  |

Views:

- Grid view (grid)
```

And the matching CSV rows:

```
base,table,field,field_id,type,details,description,is_primary
Client CRM,Clients,Name,fldAAAAAAAAAAAAAA,singleLineText,,Legal name of the client,true
Client CRM,Clients,Status,fldBBBBBBBBBBBBBB,singleSelect,"choices: Lead, Active, Churned",,false
```

## Limits

- **Rate limit.** Requests are spaced 220 ms apart, under Airtable's 5 requests per second. HTTP 429 is retried after 30 seconds (or the `Retry-After` value) and 5xx responses with a growing wait, up to 6 times.
- **CSV.** UTF-8 without a BOM, RFC 4180 quoting with CRLF line endings. Cells that start with `=`, `+`, `-`, `@`, a tab or a carriage return get a leading `'` so spreadsheets do not run them as formulas. Programs that read the CSV should strip that one quote if they need the original text.
- **Markdown.** Pipes in cells are escaped and line breaks become `<br>`.
- **Schema only.** Records, comments, automations, interfaces and permissions are not exported. Airtable's metadata API does not return view filters or sorts either.
- **Base name.** The metadata API returns the base name only in the base list, so it costs one extra request.

## Tests

```
python3 -m unittest discover -s tests -v
```

The tests mock all HTTP (no network, no token needed) and cover pagination, linked-table resolution, Markdown and CSV rendering, CSV escaping, 429 retry, and 401 handling.

## Related

- [Airtable schema export in the browser](https://viewcourier.com/tools/airtable-schema-export?utm_source=github&utm_medium=readme&utm_campaign=schema_repo): the free browser version, no install.
- [ViewCourier](https://viewcourier.com/?utm_source=github&utm_medium=readme&utm_campaign=schema_repo) sends scheduled Airtable view exports as CSV, Excel or PDF by email.

## License

MIT. Copyright (c) 2026 ViewCourier
