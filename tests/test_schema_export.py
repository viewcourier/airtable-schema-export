"""Tests with a mocked HTTP layer: no network. Run: python3 -m unittest discover -s tests -v"""
import contextlib
import csv
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import airtable_schema_export as ase  # noqa: E402

BASE = "appTESTTESTTEST1"


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def ok(data):
    return Resp(json.dumps(data).encode())


def http_error(code, headers=None, body=None):
    return urllib.error.HTTPError("http://x", code, "err", headers or {}, io.BytesIO(json.dumps(body or {}).encode()))


def schema():
    return {"tables": [
        {"id": "tblA", "name": "Clients", "primaryFieldId": "fldN", "description": "Our customers", "fields": [
            {"id": "fldN", "name": "Name", "type": "singleLineText", "description": "Legal name"},
            {"id": "fldS", "name": "Status", "type": "singleSelect", "options": {"choices": [{"id": "s1", "name": "Active"}, {"id": "s2", "name": "Churned"}]}},
            {"id": "fldO", "name": "Orders", "type": "multipleRecordLinks", "options": {"linkedTableId": "tblB", "inverseLinkFieldId": "fldC", "prefersSingleRecordLink": False}},
            {"id": "fldP", "name": "Total", "type": "currency", "options": {"precision": 2, "symbol": "€"}},
            {"id": "fldF", "name": "Label", "type": "formula", "options": {"formula": "{Name} & \"|\" & {Status}", "referencedFieldIds": ["fldN", "fldS"], "result": {"type": "singleLineText"}}},
        ], "views": [{"id": "viw1", "name": "All clients", "type": "grid"}, {"id": "viw2", "name": "Board", "type": "kanban"}]},
        {"id": "tblB", "name": "Orders", "primaryFieldId": "fldT", "fields": [
            {"id": "fldT", "name": "Ref", "type": "singleLineText"},
            {"id": "fldC", "name": "Client", "type": "multipleRecordLinks", "options": {"linkedTableId": "tblA"}},
        ], "views": [{"id": "viw3", "name": "Grid view", "type": "grid"}]},
    ]}


class FakeNet:
    """Replaces urllib.request.urlopen; `script` maps a URL substring to a list of responses/exceptions."""

    def __init__(self, script):
        self.script, self.calls = script, []

    def __call__(self, req, timeout=None):
        self.calls.append(req)
        for key, queue in self.script.items():
            if key in req.full_url and queue:
                item = queue.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item
        raise AssertionError("unexpected request " + req.full_url)


class Base(unittest.TestCase):
    def setUp(self):
        for target in ("MIN_INTERVAL", "BACKOFF_BASE"):
            p = mock.patch.object(ase, target, 0)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(ase.time, "sleep")
        self.sleep = p.start()
        self.addCleanup(p.stop)

    def client_with(self, script):
        net = FakeNet(script)
        p = mock.patch.object(ase.urllib.request, "urlopen", net)
        p.start()
        self.addCleanup(p.stop)
        return ase.Client("patTESTTOKEN"), net


class TestHttp(Base):
    def test_list_bases_pagination(self):
        c, net = self.client_with({"/meta/bases": [
            ok({"bases": [{"id": "app1", "name": "One"}], "offset": "itrNEXT/app1"}),
            ok({"bases": [{"id": "app2", "name": "Two"}]})]})
        bases = ase.list_bases(c)
        self.assertEqual([b["id"] for b in bases], ["app1", "app2"])
        self.assertNotIn("offset", net.calls[0].full_url)
        self.assertIn("offset=itrNEXT%2Fapp1", net.calls[1].full_url)
        self.assertEqual(net.calls[0].get_header("Authorization"), "Bearer patTESTTOKEN")

    def test_429_is_retried(self):
        c, net = self.client_with({"/tables": [http_error(429, {"Retry-After": "3"}), http_error(503), ok(schema())]})
        data = ase.fetch_schema(c, BASE)
        self.assertEqual(len(data["tables"]), 2)
        self.assertEqual(len(net.calls), 3)
        self.assertIn(3.0, [call.args[0] for call in self.sleep.call_args_list])

    def test_429_gives_up(self):
        c, net = self.client_with({"/tables": [http_error(429)] * (ase.MAX_RETRIES + 1)})
        with self.assertRaises(ase.AirtableError) as cm:
            ase.fetch_schema(c, BASE)
        self.assertIn("429", str(cm.exception))
        self.assertEqual(len(net.calls), ase.MAX_RETRIES + 1)

    def test_401_clear_error_no_retry(self):
        c, net = self.client_with({"/tables": [http_error(401, body={"error": {"type": "AUTHENTICATION_REQUIRED"}})]})
        with self.assertRaises(ase.AirtableError) as cm:
            ase.fetch_schema(c, BASE)
        self.assertIn("401", str(cm.exception))
        self.assertIn("token", str(cm.exception))
        self.assertNotIn("patTESTTOKEN", str(cm.exception))
        self.assertEqual(len(net.calls), 1)

    def test_403_and_404_messages(self):
        self.assertIn("schema.bases:read", ase.explain_http_error(403))
        self.assertIn("base ID", ase.explain_http_error(404))

    def test_base_name_falls_back_to_id(self):
        c, _ = self.client_with({"/meta/bases": [http_error(403)]})
        self.assertEqual(ase.find_base_name(c, BASE), BASE)


class TestRender(unittest.TestCase):
    def test_linked_table_resolved(self):
        tables, fields = ase.build_lookup(schema())
        f = schema()["tables"][0]["fields"][2]
        d = ase.details_text(f, "Clients", tables, fields)
        self.assertIn("links to: Orders", d)
        self.assertIn("inverse field: Orders.Client", d)
        self.assertNotIn("tblB", d)

    def test_select_number_formula_details(self):
        tables, fields = ase.build_lookup(schema())
        fs = schema()["tables"][0]["fields"]
        self.assertEqual(ase.details_text(fs[1], "Clients", tables, fields), "choices: Active, Churned")
        self.assertEqual(ase.details_text(fs[3], "Clients", tables, fields), "precision: 2; symbol: €")
        d = ase.details_text(fs[4], "Clients", tables, fields)
        self.assertIn("references: Name, Status", d)
        self.assertIn("result: singleLineText", d)

    def test_markdown(self):
        md = ase.render_markdown(schema(), BASE, "Demo Base", "2026-01-01T00:00:00Z")
        self.assertIn("## Clients", md)
        self.assertIn("## Orders", md)
        self.assertIn("Primary field: **Name**", md)
        self.assertIn("| Field | Type | Details | Description |", md)
        self.assertIn("| Status | singleSelect | choices: Active, Churned |  |", md)
        self.assertIn("| Name | singleLineText |  | Legal name |", md)
        self.assertIn("- All clients (grid)", md)
        self.assertIn("- Board (kanban)", md)
        self.assertIn("Our customers", md)
        # a pipe inside a formula must be escaped so the table does not break
        self.assertIn('\\|', md)

    def test_markdown_newline_in_description(self):
        s = {"tables": [{"id": "t", "name": "T", "primaryFieldId": "f", "fields": [
            {"id": "f", "name": "A", "type": "singleLineText", "description": "line1\nline2"}]}]}
        self.assertIn("line1<br>line2", ase.render_markdown(s, BASE, "B", "x"))

    def test_csv_rows(self):
        text = ase.render_csv(schema(), "Demo Base")
        self.assertFalse(text.startswith("\ufeff"))
        rows = list(csv.reader(io.StringIO(text, newline="")))
        self.assertEqual(rows[0], ase.CSV_COLUMNS)
        self.assertEqual(len(rows), 1 + 5 + 2)
        name = rows[1]
        self.assertEqual(name[:5], ["Demo Base", "Clients", "Name", "fldN", "singleLineText"])
        self.assertEqual((name[6], name[7]), ("Legal name", "true"))
        self.assertEqual(rows[2][7], "false")
        self.assertIn("links to: Orders", rows[3][5])
        self.assertIn("\r\n", text)

    def test_csv_escaping(self):
        for bad in ("=1+1", "+1", "-1", "@SUM(A1)", "\tx", "\rx"):
            self.assertEqual(ase.csv_safe(bad), "'" + bad)
        for good in ("abc", "", "a=b", None):
            self.assertEqual(ase.csv_safe(good), good or "")
        s = {"tables": [{"id": "t", "name": "T", "primaryFieldId": "f", "fields": [
            {"id": "f", "name": "=HYPERLINK(\"x\")", "type": "singleLineText", "description": "has, comma\nand \"quotes\""}]}]}
        text = ase.render_csv(s, "B")
        rows = list(csv.reader(io.StringIO(text, newline="")))
        self.assertEqual(rows[1][2], "'=HYPERLINK(\"x\")")
        self.assertEqual(rows[1][6], "has, comma\nand \"quotes\"")
        self.assertIn('"has, comma\nand ""quotes"""', text)

    def test_json_contains_raw_and_timestamp(self):
        data = json.loads(ase.render_json(schema(), BASE, "Demo", "2026-01-01T00:00:00Z"))
        self.assertEqual(data["generated_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(data["tables"], schema()["tables"])

    def test_slug(self):
        self.assertEqual(ase.slugify("ViewCourier Demo!"), "viewcourier-demo")
        self.assertEqual(ase.slugify("!!!", "appx"), "appx")


class TestCli(Base):
    def run_cli(self, argv, script, env=None):
        net = FakeNet(script)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(ase.urllib.request, "urlopen", net), \
                mock.patch.dict(os.environ, env if env is not None else {"AIRTABLE_TOKEN": "patTESTTOKEN"}, clear=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = ase.main(argv)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue(), net

    def test_export_all_formats(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, err, net = self.run_cli(
                ["--base", BASE, "--out", d],
                {"/tables": [ok(schema())], "/meta/bases": [ok({"bases": [{"id": BASE, "name": "Demo Base"}]})]})
            self.assertEqual(code, 0, err)
            self.assertEqual(sorted(os.listdir(d)), ["demo-base-schema.csv", "demo-base-schema.json", "demo-base-schema.md"])
            with open(os.path.join(d, "demo-base-schema.csv"), "rb") as fh:
                self.assertFalse(fh.read().startswith(b"\xef\xbb\xbf"))
            self.assertIn("2 table(s), 7 field(s)", out)

    def test_single_format(self):
        with tempfile.TemporaryDirectory() as d:
            code, *_ = self.run_cli(["--base", BASE, "--format", "md", "--out", d],
                                    {"/tables": [ok(schema())], "/meta/bases": [ok({"bases": []})]})
            self.assertEqual(code, 0)
            self.assertEqual(os.listdir(d), [BASE.lower() + "-schema.md"])

    def test_list_bases(self):
        code, out, err, _ = self.run_cli(["--list-bases"], {"/meta/bases": [ok({"bases": [
            {"id": "app1", "name": "One", "permissionLevel": "read"}]})]})
        self.assertEqual(code, 0)
        self.assertIn("app1\tOne\tread", out)

    def test_401_exit_code_and_no_token_leak(self):
        code, out, err, _ = self.run_cli(["--base", BASE], {"/tables": [http_error(401)]})
        self.assertEqual(code, 1)
        self.assertIn("401", err)
        self.assertNotIn("patTESTTOKEN", err + out)

    def test_missing_token(self):
        code, out, err, net = self.run_cli(["--base", BASE], {}, env={})
        self.assertEqual(code, 2)
        self.assertEqual(net.calls, [])

    def test_missing_base(self):
        code, *_ = self.run_cli([], {})
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
