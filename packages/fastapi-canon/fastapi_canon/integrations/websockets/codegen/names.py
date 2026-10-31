from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from fastapi_canon.integrations.websockets.codegen.ir import (
    ApiNode,
    BinaryStreamDecl,
    ClientIr,
    NotificationDecl,
    RouteDecl,
    SocketDecl,
    UnsupportedSchemaError,
)


@dataclass(frozen=True, slots=True)
class ApiView:
    root_operations: tuple[RouteDecl, ...]
    nodes: tuple[ApiViewNode, ...]


@dataclass(frozen=True, slots=True)
class ApiViewNode:
    segment: str
    path: tuple[str, ...]
    source_path: tuple[str, ...]
    operations: tuple[RouteDecl, ...]
    children: tuple[ApiViewNode, ...]


@dataclass(frozen=True, slots=True)
class EventView:
    root_events: tuple[NotificationDecl, ...]
    nodes: tuple[EventViewNode, ...]


@dataclass(frozen=True, slots=True)
class EventViewNode:
    segment: str
    path: tuple[str, ...]
    source_path: tuple[str, ...]
    events: tuple[NotificationDecl, ...]
    children: tuple[EventViewNode, ...]


@dataclass(frozen=True, slots=True)
class ClientView:
    root_operations: tuple[RouteDecl, ...]
    root_notifications: tuple[NotificationDecl, ...]
    root_streams: tuple[BinaryStreamDecl, ...]
    nodes: tuple[NamespaceViewNode, ...]
    root_sockets: tuple[SocketDecl, ...] = ()


@dataclass(frozen=True, slots=True)
class NamespaceViewNode:
    segment: str
    path: tuple[str, ...]
    source_path: tuple[str, ...]
    operations: tuple[RouteDecl, ...]
    notifications: tuple[NotificationDecl, ...]
    streams: tuple[BinaryStreamDecl, ...]
    children: tuple[NamespaceViewNode, ...]
    sockets: tuple[SocketDecl, ...] = ()


def api_view(
    ir: ClientIr,
    *,
    api_root: str | None,
    api_names: Mapping[str, str],
) -> ApiView:
    root = tuple(api_root.split(".")) if api_root else ()
    root_exists = _root_exists(ir.api, root) or any(
        notification.path[: len(root)] == root for notification in ir.notifications
    )
    if api_root and (any(not part for part in root) or not root_exists):
        raise UnsupportedSchemaError(
            f"api_root {api_root!r} does not identify an API path in the contract"
        )

    matched_names: set[str] = set()
    root_operations = list(ir.root_operations)
    projected: list[tuple[tuple[str, ...], tuple[str, ...], RouteDecl]] = []
    for route in ir.operations:
        if not route.path:
            continue
        visible_source = (
            route.path[len(root) :]
            if root and route.path[: len(root)] == root
            else route.path
        )
        visible = tuple(
            _api_name(
                route.path,
                visible_source,
                index,
                api_names,
                matched_names,
            )
            for index in range(len(visible_source))
        )
        if not visible:
            root_operations.append(route)
        else:
            projected.append((visible, route.path, route))

    matched_names.update(_matching_notification_aliases(ir, root, api_names))
    unknown = sorted(set(api_names) - matched_names)
    if unknown:
        rendered = ", ".join(repr(name) for name in unknown)
        raise UnsupportedSchemaError(
            f"api_names contains paths that do not exist in the contract: {rendered}"
        )
    visible_sources: dict[tuple[str, ...], tuple[str, ...]] = {}
    for visible, source, _ in projected:
        source_offset = len(source) - len(visible)
        for length in range(1, len(visible) + 1):
            visible_prefix = visible[:length]
            source_prefix = source[: source_offset + length]
            previous = visible_sources.get(visible_prefix)
            if previous is not None and previous != source_prefix:
                raise UnsupportedSchemaError(
                    f"API paths {'.'.join(previous)!r} and "
                    f"{'.'.join(source_prefix)!r} both map to "
                    f"{'.'.join(visible_prefix)!r}"
                )
            visible_sources[visible_prefix] = source_prefix
    return ApiView(tuple(root_operations), _view_tree(projected))


def event_view(
    ir: ClientIr,
    *,
    api_root: str | None,
    api_names: Mapping[str, str],
) -> EventView:
    root = tuple(api_root.split(".")) if api_root else ()
    root_events: list[NotificationDecl] = []
    projected: list[tuple[tuple[str, ...], tuple[str, ...], NotificationDecl]] = []
    for event in ir.notifications:
        source_path = event.path
        visible_source = (
            source_path[len(root) :]
            if root and source_path[: len(root)] == root
            else source_path
        )
        visible = tuple(
            _api_name(source_path, visible_source, index, api_names, set())
            for index in range(len(visible_source))
        )
        if not visible:
            root_events.append(event)
        else:
            projected.append((visible, source_path, event))
    return EventView(tuple(root_events), _event_tree(projected))


def client_view(
    ir: ClientIr,
    *,
    api_root: str | None,
    api_names: Mapping[str, str],
) -> ClientView:
    routes = api_view(ir, api_root=api_root, api_names=api_names)
    events = event_view(ir, api_root=api_root, api_names=api_names)
    root_streams: list[BinaryStreamDecl | SocketDecl] = []
    projected_streams: list[
        tuple[tuple[str, ...], tuple[str, ...], BinaryStreamDecl | SocketDecl]
    ] = []
    root = tuple(api_root.split(".")) if api_root else ()
    for stream in (*ir.binary_streams, *ir.sockets):
        visible = (
            stream.path[len(root) :]
            if root and stream.path[: len(root)] == root
            else stream.path
        )
        if not visible:
            root_streams.append(stream)
        else:
            projected_streams.append((visible, stream.path, stream))
    return ClientView(
        root_operations=routes.root_operations,
        root_notifications=events.root_events,
        root_streams=tuple(_binary(root_streams)),
        nodes=_merge_nodes(routes.nodes, events.nodes, _stream_tree(projected_streams)),
        root_sockets=tuple(_socket_items(root_streams)),
    )


def assert_unique_names(
    owner: str,
    values: Iterable[tuple[str, str]],
) -> None:
    identifiers: dict[str, str] = {}
    for source, identifier in values:
        previous = identifiers.get(identifier)
        if previous is not None and previous != source:
            raise UnsupportedSchemaError(
                f"{owner} maps both {previous!r} and {source!r} to "
                f"identifier {identifier!r}"
            )
        identifiers[identifier] = source


def snake_case(value: str) -> str:
    parts = _words(value)
    return "_".join(part.lower() for part in parts)


def camel_case(value: str) -> str:
    parts = _words(value)
    if not parts:
        return ""
    first, *rest = parts
    return first.lower() + "".join(part[:1].upper() + part[1:].lower() for part in rest)


def pascal_case(value: str) -> str:
    return "".join(part[:1].upper() + part[1:].lower() for part in _words(value))


def _words(value: str) -> tuple[str, ...]:
    separated = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", value)
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", separated)
    return tuple(part for part in re.split(r"[^A-Za-z0-9]+", separated) if part)


def _root_exists(nodes: tuple[ApiNode, ...], root: tuple[str, ...]) -> bool:
    candidates = nodes
    for segment in root:
        node = next((item for item in candidates if item.segment == segment), None)
        if node is None:
            return False
        candidates = node.children
    return True


def _api_name(
    source_path: tuple[str, ...],
    visible_source: tuple[str, ...],
    index: int,
    aliases: Mapping[str, str],
    matched: set[str],
) -> str:
    root_length = len(source_path) - len(visible_source)
    original_prefix = ".".join(source_path[: root_length + index + 1])
    visible_prefix = ".".join(visible_source[: index + 1])
    segment = visible_source[index]
    for key in (original_prefix, visible_prefix, segment):
        if key in aliases:
            matched.add(key)
            return aliases[key]
    return segment


def _view_tree(
    routes: list[tuple[tuple[str, ...], tuple[str, ...], RouteDecl]],
) -> tuple[ApiViewNode, ...]:
    tree: dict[str, dict[str, object]] = {}
    for visible_path, source_path, route in routes:
        cursor = tree
        for index, segment in enumerate(visible_path):
            node = cursor.setdefault(
                segment,
                {
                    "source_path": source_path[
                        : len(source_path) - len(visible_path) + index + 1
                    ],
                    "operations": [],
                    "children": {},
                },
            )
            if index == len(visible_path) - 1:
                operations = node["operations"]
                assert isinstance(operations, list)
                operations.append(route)
            children = node["children"]
            assert isinstance(children, dict)
            cursor = children
    return tuple(_view_node(segment, node, ()) for segment, node in tree.items())


def _view_node(
    segment: str,
    value: dict[str, object],
    parent: tuple[str, ...],
) -> ApiViewNode:
    children = value["children"]
    operations = value["operations"]
    source_path = value["source_path"]
    assert isinstance(children, dict)
    assert isinstance(operations, list)
    assert isinstance(source_path, tuple)
    path = (*parent, segment)
    return ApiViewNode(
        segment=segment,
        path=path,
        source_path=source_path,
        operations=tuple(operations),
        children=tuple(
            _view_node(child_segment, child, path)
            for child_segment, child in children.items()
        ),
    )


def _matching_notification_aliases(
    ir: ClientIr,
    root: tuple[str, ...],
    aliases: Mapping[str, str],
) -> set[str]:
    matched: set[str] = set()
    for event in ir.notifications:
        visible = (
            event.path[len(root) :]
            if root and event.path[: len(root)] == root
            else event.path
        )
        for index in range(len(visible)):
            _api_name(event.path, visible, index, aliases, matched)
    return matched


def _event_tree(
    events: list[tuple[tuple[str, ...], tuple[str, ...], NotificationDecl]],
) -> tuple[EventViewNode, ...]:
    tree: dict[str, dict[str, object]] = {}
    for visible_path, source_path, event in events:
        cursor = tree
        for index, segment in enumerate(visible_path):
            node = cursor.setdefault(
                segment,
                {
                    "source_path": source_path[
                        : len(source_path) - len(visible_path) + index + 1
                    ],
                    "events": [],
                    "children": {},
                },
            )
            if index == len(visible_path) - 1:
                node_events = node["events"]
                assert isinstance(node_events, list)
                node_events.append(event)
            children = node["children"]
            assert isinstance(children, dict)
            cursor = children
    return tuple(_event_node(segment, node, ()) for segment, node in tree.items())


def _event_node(
    segment: str,
    value: dict[str, object],
    parent: tuple[str, ...],
) -> EventViewNode:
    children = value["children"]
    events = value["events"]
    source_path = value["source_path"]
    assert isinstance(children, dict)
    assert isinstance(events, list)
    assert isinstance(source_path, tuple)
    path = (*parent, segment)
    return EventViewNode(
        segment=segment,
        path=path,
        source_path=source_path,
        events=tuple(events),
        children=tuple(
            _event_node(child_segment, child, path)
            for child_segment, child in children.items()
        ),
    )


def _merge_nodes(
    routes: tuple[ApiViewNode, ...],
    events: tuple[EventViewNode, ...],
    streams: tuple[StreamViewNode, ...] = (),
) -> tuple[NamespaceViewNode, ...]:
    route_by_segment = {node.segment: node for node in routes}
    event_by_segment = {node.segment: node for node in events}
    stream_by_segment = {node.segment: node for node in streams}
    segments = tuple(
        dict.fromkeys((*route_by_segment, *event_by_segment, *stream_by_segment))
    )
    merged: list[NamespaceViewNode] = []
    for segment in segments:
        route = route_by_segment.get(segment)
        event = event_by_segment.get(segment)
        stream = stream_by_segment.get(segment)
        present = route or event or stream
        assert present is not None
        path = present.path
        source_path = present.source_path
        merged.append(
            NamespaceViewNode(
                segment=segment,
                path=path,
                source_path=source_path,
                operations=route.operations if route is not None else (),
                notifications=event.events if event is not None else (),
                streams=tuple(_binary(stream.streams)) if stream is not None else (),
                sockets=tuple(_socket_items(stream.streams))
                if stream is not None
                else (),
                children=_merge_nodes(
                    route.children if route is not None else (),
                    event.children if event is not None else (),
                    stream.children if stream is not None else (),
                ),
            )
        )
    return tuple(merged)


@dataclass(frozen=True, slots=True)
class StreamViewNode:
    segment: str
    path: tuple[str, ...]
    source_path: tuple[str, ...]
    streams: tuple[BinaryStreamDecl | SocketDecl, ...]
    children: tuple[StreamViewNode, ...]


def _binary(
    items: Iterable[BinaryStreamDecl | SocketDecl],
) -> Iterable[BinaryStreamDecl]:
    return (item for item in items if isinstance(item, BinaryStreamDecl))


def _socket_items(
    items: Iterable[BinaryStreamDecl | SocketDecl],
) -> Iterable[SocketDecl]:
    return (item for item in items if isinstance(item, SocketDecl))


def _stream_tree(
    streams: list[
        tuple[tuple[str, ...], tuple[str, ...], BinaryStreamDecl | SocketDecl]
    ],
) -> tuple[StreamViewNode, ...]:
    tree: dict[str, dict[str, object]] = {}
    for visible_path, source_path, stream in streams:
        cursor = tree
        for index, segment in enumerate(visible_path):
            node = cursor.setdefault(
                segment,
                {
                    "source_path": source_path[
                        : len(source_path) - len(visible_path) + index + 1
                    ],
                    "streams": [],
                    "children": {},
                },
            )
            if index == len(visible_path) - 1:
                node_streams = node["streams"]
                assert isinstance(node_streams, list)
                node_streams.append(stream)
            children = node["children"]
            assert isinstance(children, dict)
            cursor = children
    return tuple(_stream_node(segment, node, ()) for segment, node in tree.items())


def _stream_node(
    segment: str,
    value: dict[str, object],
    parent: tuple[str, ...],
) -> StreamViewNode:
    children = value["children"]
    streams = value["streams"]
    source_path = value["source_path"]
    assert isinstance(children, dict)
    assert isinstance(streams, list)
    assert isinstance(source_path, tuple)
    path = (*parent, segment)
    return StreamViewNode(
        segment,
        path,
        source_path,
        tuple(streams),
        tuple(
            _stream_node(child_segment, child, path)
            for child_segment, child in children.items()
        ),
    )
