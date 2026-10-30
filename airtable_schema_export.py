#!/usr/bin/env python3
"""airtable-schema-export: export the schema of an Airtable base (tables, fields, field types,
select choices, linked tables, views, primary field, descriptions) to JSON, a Markdown data
dictionary and CSV. Standard library only, Python 3.9+.

    AIRTABLE_TOKEN=... python3 airtable_schema_export.py --list-bases
    AIRTABLE_TOKEN=... python3 airtable_schema_export.py --base appXXXXXXXXXXXXXX [--format all] [--out DIR]

Needs a personal access token with the schema.bases:read scope.

Copyright (c) 2026 BASIT B.V. - MIT License
"""

import argparse
import csv
import datetime
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.0.0"
API = "https://api.airtable.com/v0"
MIN_INTERVAL = 0.22   # seconds between requests: about 4.5 per second, under the 5/s limit
MAX_RETRIES = 6       # for HTTP 429 and 5xx
BACKOFF_BASE = 30.0   # Airtable asks clients to wait 30 s after a 429
CSV_COLUMNS = ["base", "table", "field", "field_id", "type", "details", "description", "is_primary"]
FORMATS = ("json", "md", "csv")

EXIT_OK, EXIT_ERROR, EXIT_USAGE = 0, 1, 2


class AirtableError(Exception):
    pass


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Client:
    """Minimal Airtable metadata API client with rate spacing and retries."""

    def __init__(self, token):
        self.token = token
        self._last = 0.0

    def get(self, path, params=None):
        url = API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        for attempt in range(MAX_RETRIES + 1):
            wait = MIN_INTERVAL - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            req = urllib.request.Request(url, headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/json",
                "User-Agent": "airtable-schema-export/" + VERSION,
            })
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                code = e.code
                body = _read_error(e)
                if code == 429 or code >= 500:
                    if attempt >= MAX_RETRIES:
                        raise AirtableError("Airtable kept answering HTTP %d after %d retries: %s" % (code, MAX_RETRIES, body))
                    delay = BACKOFF_BASE if code == 429 else min(2.0 * (2 ** attempt), 60.0)
                    retry_after = e.headers.get("Retry-After") if e.headers else None
                    try:
                        if retry_after is not None:
                            delay = max(float(retry_after), 0.0)
                    except ValueError:
                        pass
                    time.sleep(delay)
                    continue
                raise AirtableError(explain_http_error(code, body))
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt >= MAX_RETRIES:
                    raise AirtableError("Network error talking to Airtable: %s" % getattr(e, "reason", e))
                time.sleep(min(2.0 * (2 ** attempt), 60.0))
        raise AirtableError("Unreachable")  # pragma: no cover


def _read_error(e):
    try:
        data = json.loads(e.read().decode("utf-8", "replace"))
        err = data.get("error")
        if isinstance(err, dict):
            return "%s: %s" % (err.get("type", ""), err.get("message", "")) if err.get("message") else str(err.get("type", ""))
        return str(err or "")
    except Exception:
        return ""


def explain_http_error(code, body=""):
    detail = (" (%s)" % body) if body else ""
    if code == 401:
        return "HTTP 401: Airtable rejected the token. Check AIRTABLE_TOKEN / --token; it must be a personal access token that has not been revoked or expired." + detail
    if code == 403:
        return ("HTTP 403: the token is not allowed to read this. It needs the schema.bases:read scope "
                "and access to the base (or its workspace) in the token settings." + detail)
    if code == 404:
        return "HTTP 404: base not found. Check the base ID (starts with 'app') and that the token has access to that base." + detail
    return "HTTP %d from Airtable%s" % (code, detail)


def list_bases(client):
    bases, offset = [], None
    while True:
        data = client.get("/meta/bases", {"offset": offset} if offset else None)
        bases.extend(data.get("bases", []))
        offset = data.get("offset")
        if not offset:
            return bases


def fetch_schema(client, base_id):
    # The tables endpoint is not paginated today; follow `offset` anyway if Airtable ever returns one.
    tables, offset = [], None
    while True:
        params = {"offset": offset} if offset else None
        data = client.get("/meta/bases/%s/tables" % urllib.parse.quote(base_id, safe=""), params)
        tables.extend(data.get("tables", []))
        offset = data.get("offset")
        if not offset:
            return {"tables": tables}


def find_base_name(client, base_id):
    """Base name for file names and CSV; falls back to the base ID if it cannot be looked up."""
    try:
        for b in list_bases(client):
            if b.get("id") == base_id:
                return b.get("name") or base_id
    except AirtableError:
        pass
    return base_id


# ---------------------------------------------------------------------------
# Field details
# ---------------------------------------------------------------------------

def build_lookup(schema):
    """Maps for resolving ids: table id -> name, field id -> 'Table.Field'."""
    tables, fields = {}, {}
    for t in schema.get("tables", []):
        tables[t["id"]] = t.get("name", t["id"])
        for f in t.get("fields", []):
            fields[f["id"]] = "%s.%s" % (t.get("name", t["id"]), f.get("name", f["id"]))
    return tables, fields


def _field_names(ids, fields, table):
    out = []
    for i in ids or []:
        name = fields.get(i, i)
        if name.startswith(table + "."):
            name = name[len(table) + 1:]
        out.append(name)
    return out


def _scalar(v):
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, dict):
        return v.get("name") or v.get("type") or json.dumps(v, sort_keys=True)
    return str(v)


def describe_field(field, table_name, tables, fields):
    """Return a list of short 'key: value' strings describing the field options."""
    o = field.get("options") or {}
    t = field.get("type", "")
    parts = []

    if t in ("singleSelect", "multipleSelects"):
        names = [c.get("name", "") for c in o.get("choices", [])]
        parts.append("choices: " + (", ".join(names) if names else "(none)"))
    elif t == "multipleRecordLinks":
        parts.append("links to: " + tables.get(o.get("linkedTableId"), o.get("linkedTableId", "?")))
        if o.get("prefersSingleRecordLink"):
            parts.append("single record preferred")
        if o.get("inverseLinkFieldId"):
            parts.append("inverse field: " + _field_names([o["inverseLinkFieldId"]], fields, "")[0])
        if o.get("isReversed"):
            parts.append("reversed link")
    elif t in ("formula", "rollup", "lookup", "count", "multipleLookupValues"):
        if t == "formula" and o.get("formula"):
            parts.append("formula: " + o["formula"])
        if o.get("recordLinkFieldId"):
            parts.append("via link field: " + ", ".join(_field_names([o["recordLinkFieldId"]], fields, table_name)))
        if o.get("fieldIdInLinkedTable"):
            parts.append("linked field: " + ", ".join(_field_names([o["fieldIdInLinkedTable"]], fields, "")))
        if o.get("referencedFieldIds"):
            parts.append("references: " + ", ".join(_field_names(o["referencedFieldIds"], fields, table_name)))
        res = o.get("result")
        if isinstance(res, dict) and res.get("type"):
            parts.append("result: " + res["type"])
        if not o.get("isValid", True):
            parts.append("invalid")
    elif t in ("number", "percent", "currency", "rating", "duration"):
        if "precision" in o:
            parts.append("precision: %s" % o["precision"])
        if o.get("symbol"):
            parts.append("symbol: " + o["symbol"])
        if "max" in o:
            parts.append("max: %s" % o["max"])
        if o.get("icon"):
            parts.append("icon: " + o["icon"])
        if o.get("durationFormat"):
            parts.append("format: " + o["durationFormat"])
    elif t in ("date", "dateTime", "createdTime", "lastModifiedTime"):
        if o.get("dateFormat"):
            parts.append("date format: " + _scalar(o["dateFormat"]))
        if o.get("timeFormat"):
            parts.append("time format: " + _scalar(o["timeFormat"]))
        if o.get("timeZone"):
            parts.append("time zone: " + o["timeZone"])
        if o.get("referencedFieldIds"):
            parts.append("watches: " + ", ".join(_field_names(o["referencedFieldIds"], fields, table_name)))
    elif t == "checkbox":
        if o.get("icon"):
            parts.append("icon: " + o["icon"])
    else:
        for k in sorted(o):
            if k in ("color",) or o[k] is False or o[k] in (None, "", [], {}):
                continue
            v = o[k]
            parts.append("%s: %s" % (k, ", ".join(_scalar(x) for x in v) if isinstance(v, list) else _scalar(v)))
    return parts


def details_text(field, table_name, tables, fields):
    return "; ".join(describe_field(field, table_name, tables, fields))


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def slugify(name, fallback="base"):
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")
    return s[:80].strip("-") or fallback


def render_json(schema, base_id, base_name, generated_at):
    out = {"generated_at": generated_at, "base": {"id": base_id, "name": base_name}}
    out.update(schema)
    return json.dumps(out, indent=2, ensure_ascii=False) + "\n"


def md_cell(s):
    s = "" if s is None else str(s)
    return s.replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>").replace("\r", "<br>")


def render_markdown(schema, base_id, base_name, generated_at):
    tables, fields = build_lookup(schema)
    lines = ["# %s: data dictionary" % base_name, "",
             "Base ID: `%s`. Tables: %d. Generated: %s." % (base_id, len(schema.get("tables", [])), generated_at), ""]
    for t in schema.get("tables", []):
        name = t.get("name", t["id"])
        lines += ["## " + name, ""]
        if t.get("description"):
            lines += [t["description"].strip(), ""]
        primary = next((f.get("name") for f in t.get("fields", []) if f.get("id") == t.get("primaryFieldId")), None)
        lines.append("Table ID: `%s`." % t["id"] + (" Primary field: **%s**." % md_cell(primary) if primary else ""))
        lines += ["", "| Field | Type | Details | Description |", "| --- | --- | --- | --- |"]
        for f in t.get("fields", []):
            lines.append("| %s | %s | %s | %s |" % (
                md_cell(f.get("name")), md_cell(f.get("type")),
                md_cell(details_text(f, name, tables, fields)), md_cell(f.get("description", ""))))
        views = t.get("views", [])
        if views:
            lines += ["", "Views:", ""]
            lines += ["- %s (%s)" % (md_cell(v.get("name")), md_cell(v.get("type"))) for v in views]
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def csv_safe(value):
    """Neutralise spreadsheet formula injection: prefix ' if the cell starts with = + - @ TAB or CR."""
    s = "" if value is None else str(value)
    if s and s[0] in "=+-@\t\r":
        return "'" + s
    return s


def render_csv(schema, base_name):
    tables, fields = build_lookup(schema)
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    w.writerow(CSV_COLUMNS)
    for t in schema.get("tables", []):
        tname = t.get("name", t["id"])
        for f in t.get("fields", []):
            w.writerow([csv_safe(x) for x in (
                base_name, tname, f.get("name", ""), f.get("id", ""), f.get("type", ""),
                details_text(f, tname, tables, fields), f.get("description", ""),
                "true" if f.get("id") == t.get("primaryFieldId") else "false")])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def write_file(path, text):
    with open(path, "w", encoding="utf-8", newline="") as fh:   # UTF-8, no BOM
        fh.write(text)


def export(client, base_id, formats, out_dir, now=None):
    schema = fetch_schema(client, base_id)
    base_name = find_base_name(client, base_id)
    generated_at = (now or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.join(out_dir, slugify(base_name, base_id.lower()) + "-schema")
    written = []
    for fmt in formats:
        if fmt == "json":
            text = render_json(schema, base_id, base_name, generated_at)
        elif fmt == "md":
            text = render_markdown(schema, base_id, base_name, generated_at)
        else:
            text = render_csv(schema, base_name)
        path = "%s.%s" % (stem, fmt)
        write_file(path, text)
        written.append(path)
    return schema, base_name, written


def build_parser():
    p = argparse.ArgumentParser(
        prog="airtable_schema_export.py",
        description="Export an Airtable base schema (tables, fields, types, options, views) to JSON, Markdown and CSV.")
    p.add_argument("--base", metavar="BASE_ID", help="Base ID, starts with 'app'.")
    p.add_argument("--list-bases", action="store_true", help="List the bases the token can access and exit.")
    p.add_argument("--format", choices=list(FORMATS) + ["all"], default="all", help="Output format (default: all).")
    p.add_argument("--out", default=".", metavar="DIR", help="Output directory (default: current directory). Created if missing.")
    p.add_argument("--token", help="Personal access token. Prefer the AIRTABLE_TOKEN environment variable: command line arguments are visible to other users of the machine.")
    p.add_argument("--version", action="version", version="%(prog)s " + VERSION)
    return p


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    token = args.token or os.environ.get("AIRTABLE_TOKEN")
    if not args.list_bases and not args.base:
        p.error("--base BASE_ID is required (or use --list-bases)")
    if args.base and not re.fullmatch(r"app[A-Za-z0-9]{10,}", args.base):
        p.error("base ID should look like appXXXXXXXXXXXXXX (starts with 'app')")
    if not token:
        print("error: no token. Set AIRTABLE_TOKEN or pass --token (personal access token with schema.bases:read).", file=sys.stderr)
        return EXIT_USAGE
    client = Client(token)
    try:
        if args.list_bases:
            bases = list_bases(client)
            for b in bases:
                print("%s\t%s\t%s" % (b.get("id", ""), b.get("name", ""), b.get("permissionLevel", "")))
            print("%d base(s)" % len(bases), file=sys.stderr)
            return EXIT_OK
        formats = list(FORMATS) if args.format == "all" else [args.format]
        schema, base_name, written = export(client, args.base, formats, args.out)
    except AirtableError as e:
        print("error: %s" % e, file=sys.stderr)
        return EXIT_ERROR
    except OSError as e:
        print("error: cannot write output: %s" % e, file=sys.stderr)
        return EXIT_ERROR
    n_fields = sum(len(t.get("fields", [])) for t in schema["tables"])
    print("%s: %d table(s), %d field(s)" % (base_name, len(schema["tables"]), n_fields))
    for w in written:
        print("wrote " + w)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
