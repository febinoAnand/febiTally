"""XML transport for the Tally client.

The client builds every request in the JSON-API shape (headers + payload, see tally_client).
In XML mode ``to_xml`` turns that same request into a Tally XML ENVELOPE, and ``parse`` turns
the XML response into nested dicts, so the shared response parsers work for both formats.
"""
import re
import xml.etree.ElementTree as ET


class XmlError(Exception):
    """Tally returned something that is not usable XML, or an XML-level error."""


# Control characters Tally sometimes emits (e.g. &#4;) are not valid XML 1.0.
_BAD_CHAR_REFS = re.compile(r"&#(?:x0*(?:[0-8bcef]|1[0-9a-f])|0*(?:[0-8]|1[124-9]|2[0-9]|3[01]));", re.I)
_BAD_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>")


def _sub(parent, tag, text=None, **attrs):
    el = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})
    if text is not None:
        el.text = str(text)
    return el


def _tdl_name(name):
    """'Opening Balance' -> 'OpeningBalance' (TDL method names have no spaces)."""
    return name.replace(" ", "")


def _add_value(el, key, value):
    if key == "name":
        # masters carry their (new) name in NAME.LIST; the NAME attribute identifies the existing one
        _sub(_sub(el, "NAME.LIST", TYPE="String"), "NAME", value)
    elif isinstance(value, list):
        for item in value:
            child = _sub(el, key.upper() + ".LIST")
            if isinstance(item, dict):
                for k, v in item.items():
                    _add_value(child, k, v)
            else:
                child.text = str(item)
    elif isinstance(value, dict):
        child = _sub(el, key.upper())
        for k, v in value.items():
            _add_value(child, k, v)
    else:
        _sub(el, key.upper(), value)


def _add_object(parent, obj):
    meta = obj.get("metadata") or {}
    attrs = {k.upper(): v for k, v in meta.items() if k != "type"}
    el = _sub(parent, str(meta.get("type", "OBJECT")).upper(), **attrs)
    for key, value in obj.items():
        if key != "metadata":
            _add_value(el, key, value)
    return el


def to_xml(headers, payload, collection_types=None):
    """Convert a JSON-API style request into a Tally XML envelope string.

    collection_types maps built-in collection ids (e.g. 'List of Ledgers') to a TDL object
    type ('Ledger'). Those exports are sent as an inline TDL collection, which every Tally
    Prime release understands.
    """
    env = ET.Element("ENVELOPE")
    hdr = _sub(env, "HEADER")
    _sub(hdr, "VERSION", "1")
    _sub(hdr, "TALLYREQUEST", headers.get("tallyrequest", "Export"))
    req_type, req_id = headers.get("type", ""), headers.get("id", "")
    tdl_type = (collection_types or {}).get(req_id) if req_type == "Collection" else None
    if tdl_type:
        req_id = "Febi%sCollection" % tdl_type
    _sub(hdr, "TYPE", req_type)
    if headers.get("subtype"):
        _sub(hdr, "SUBTYPE", headers["subtype"])
        _sub(hdr, "ID", req_id, TYPE="Name")
    else:
        _sub(hdr, "ID", req_id)

    body = _sub(env, "BODY")
    desc = _sub(body, "DESC")
    static = _sub(desc, "STATICVARIABLES")
    for var in payload.get("static_variables") or []:
        name, value = var["name"], var["value"]
        if name.lower() == "svexportformat":
            value = "$$SysName:XML"
        _sub(static, name.upper(), value)

    fetch = [_tdl_name(f) for f in payload.get("fetch_list") or []]
    if tdl_type:
        msg = _sub(_sub(desc, "TDL"), "TDLMESSAGE")
        col = _sub(msg, "COLLECTION", NAME=req_id, ISMODIFY="No")
        _sub(col, "TYPE", tdl_type)
        _sub(col, "FETCH", ", ".join(fetch or ["Name"]))
    elif fetch:
        fl = _sub(desc, "FETCHLIST")
        for f in fetch:
            _sub(fl, "FETCH", f)

    messages = payload.get("tallymessage")
    if messages:
        data = _sub(body, "DATA")
        for obj in messages:
            _add_object(_sub(data, "TALLYMESSAGE"), obj)

    return ET.tostring(env, encoding="unicode")


def _to_dict(el):
    node = dict(el.attrib)
    for child in el:
        value = _to_dict(child)
        if child.tag in node:
            if not isinstance(node[child.tag], list):
                node[child.tag] = [node[child.tag]]
            node[child.tag].append(value)
        else:
            node[child.tag] = value
    text = (el.text or "").strip()
    if not node:
        return text
    if text:
        node["value"] = text
    return node


def clean(text):
    text = _XML_DECL.sub("", text.strip())
    return _BAD_CHARS.sub("", _BAD_CHAR_REFS.sub("", text))


def parse(text):
    """Parse a Tally XML response into nested dicts. Raises XmlError on errors."""
    cleaned = clean(text)
    if not cleaned:
        return {}
    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError:
        raise XmlError("Tally returned a non-XML response: %s" % text[:300])
    data = {root.tag: _to_dict(root)}
    content = data[root.tag]
    # <RESPONSE>Unknown Request, cannot be processed</RESPONSE>
    if root.tag == "RESPONSE" and isinstance(content, str):
        raise XmlError(content or "Tally rejected the request")
    # <ENVELOPE><HEADER><STATUS>0</STATUS></HEADER><BODY><DATA>error text</DATA></BODY></ENVELOPE>
    if isinstance(content, dict):
        header = content.get("HEADER")
        if isinstance(header, dict) and str(header.get("STATUS", "")).strip() == "0":
            body = content.get("BODY") or {}
            detail = body.get("DATA") if isinstance(body, dict) else body
            raise XmlError(str(detail or "Tally request failed")[:300])
    return data
