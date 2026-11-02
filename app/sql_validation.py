from __future__ import annotations

import re
from fastapi import HTTPException

MAX_SETUP_STATEMENTS = 30


def split_sql(source: str) -> list[str]:
    """Split SQL on semicolons outside quoted strings and comments.

    Supports SQL single/double/backtick quotes, PostgreSQL dollar quoting, SQL line
    comments and nested block comments. It is a splitter, not a SQL security parser.
    Database privileges and isolated execution containers provide the security boundary.
    """
    statements: list[str] = []
    buffer: list[str] = []
    index = 0
    state = "normal"
    block_depth = 0
    dollar_tag = ""
    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""
        pair = char + next_char

        if state == "single":
            buffer.append(char)
            if char == "\\" and index + 1 < len(source):
                index += 1
                buffer.append(source[index])
            elif char == "'":
                if next_char == "'":
                    index += 1
                    buffer.append(source[index])
                else:
                    state = "normal"
        elif state == "double":
            buffer.append(char)
            if char == '"':
                if next_char == '"':
                    index += 1
                    buffer.append(source[index])
                else:
                    state = "normal"
        elif state == "backtick":
            buffer.append(char)
            if char == "`":
                if next_char == "`":
                    index += 1
                    buffer.append(source[index])
                else:
                    state = "normal"
        elif state == "line_comment":
            buffer.append(char)
            if char == "\n":
                state = "normal"
        elif state == "block_comment":
            if pair == "/*":
                block_depth += 1
                buffer.extend(pair)
                index += 1
            elif pair == "*/":
                block_depth -= 1
                buffer.extend(pair)
                index += 1
                if block_depth == 0:
                    state = "normal"
            else:
                buffer.append(char)
        elif state == "dollar":
            if source.startswith(dollar_tag, index):
                buffer.extend(dollar_tag)
                index += len(dollar_tag) - 1
                state = "normal"
            else:
                buffer.append(char)
        else:
            if pair == "--" or char == "#":
                state = "line_comment"
                if pair == "--":
                    buffer.extend(pair)
                    index += 1
                else:
                    buffer.append(char)
            elif pair == "/*":
                state = "block_comment"
                block_depth = 1
                buffer.extend(pair)
                index += 1
            elif char == "'":
                state = "single"
                buffer.append(char)
            elif char == '"':
                state = "double"
                buffer.append(char)
            elif char == "`":
                state = "backtick"
                buffer.append(char)
            elif char == "$":
                match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", source[index:])
                if match:
                    dollar_tag = match.group(0)
                    state = "dollar"
                    buffer.extend(dollar_tag)
                    index += len(dollar_tag) - 1
                else:
                    buffer.append(char)
            elif char == ";":
                part = "".join(buffer).strip()
                if part:
                    statements.append(part)
                buffer.clear()
            else:
                buffer.append(char)
        index += 1

    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return statements


def _reject_client_commands(source: str) -> None:
    # Database shells have their own dot/backslash commands. The runner accepts SQL only.
    if re.search(r"(?m)^\s*(?:\.|\\)[A-Za-z_]", source):
        raise HTTPException(status_code=422, detail="Database shell commands are not accepted.")


def validate_database_sql(setup_sql: str, query: str) -> tuple[list[str], str]:
    _reject_client_commands(setup_sql)
    _reject_client_commands(query)
    setup_statements = split_sql(setup_sql)
    if len(setup_statements) > MAX_SETUP_STATEMENTS:
        raise HTTPException(status_code=413, detail="Setup SQL contains too many statements.")
    query_statements = split_sql(query)
    if len(query_statements) != 1:
        raise HTTPException(
            status_code=422,
            detail="Submit exactly one SQL statement in query. Put table creation and seed data in setup_sql.",
        )
    return setup_statements, query_statements[0]
