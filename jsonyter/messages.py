"""Builders for Jupyter messaging protocol (v5.3) messages.

The Jupyter kernel wire protocol is documented at
https://jupyter-client.readthedocs.io/en/stable/messaging.html — over the
server's ``/api/kernels/<id>/channels`` WebSocket every message is a single
JSON object with a ``channel`` field, which is what these helpers produce.
"""

import datetime
import uuid

PROTOCOL_VERSION = "5.3"


def new_id():
    return uuid.uuid4().hex


def make_header(msg_type, session, username="jsonyter"):
    return {
        "msg_id": new_id(),
        "session": session,
        "username": username,
        "msg_type": msg_type,
        "version": PROTOCOL_VERSION,
        "date": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def make_message(msg_type, session, content=None, channel="shell"):
    return {
        "header": make_header(msg_type, session),
        "parent_header": {},
        "metadata": {},
        "content": content if content is not None else {},
        "buffers": [],
        "channel": channel,
    }


def execute_request(session, code, silent=False, store_history=True,
                    user_expressions=None, allow_stdin=False,
                    stop_on_error=True):
    return make_message("execute_request", session, {
        "code": code,
        "silent": silent,
        "store_history": store_history,
        "user_expressions": user_expressions or {},
        "allow_stdin": allow_stdin,
        "stop_on_error": stop_on_error,
    })


def complete_request(session, code, cursor_pos=None):
    return make_message("complete_request", session, {
        "code": code,
        "cursor_pos": len(code) if cursor_pos is None else cursor_pos,
    })


def inspect_request(session, code, cursor_pos=None, detail_level=0):
    return make_message("inspect_request", session, {
        "code": code,
        "cursor_pos": len(code) if cursor_pos is None else cursor_pos,
        "detail_level": detail_level,
    })


def is_complete_request(session, code):
    return make_message("is_complete_request", session, {"code": code})


def kernel_info_request(session):
    return make_message("kernel_info_request", session)


def history_request(session, output=False, raw=True, hist_access_type="tail",
                    n=50, **extra):
    content = {
        "output": output,
        "raw": raw,
        "hist_access_type": hist_access_type,
        "n": n,
    }
    content.update(extra)
    return make_message("history_request", session, content)


def input_reply(session, value):
    return make_message("input_reply", session, {"value": value},
                        channel="stdin")
