# Coding Style Guide

Dieser Guide ist verbindlich für Library-Code, Tests und Beispiele. Im Zweifel
gewinnen Lesbarkeit, explizites Verhalten und eine kleine öffentliche API.

## Python

- Unterstützt werden CPython 3.12 bis 3.14.
- Moderne Python-Syntax ist erwünscht: `type`-Aliases, PEP-695-Generics,
  Union-Syntax mit `|`, Pattern Matching und `strict=True`, wo passend.
- `from __future__ import annotations` wird nur importiert, wenn es tatsächlich
  benötigt wird.
- Die Zeilenlänge beträgt 88 Zeichen. Ruff formatiert und sortiert Imports.
- Code, Bezeichner, Fehlermeldungen und Docstrings sind auf Englisch.

## Typisierung

- Produktionscode und Tests laufen unter strengem mypy.
- Jede öffentliche Funktion, Methode und jedes öffentliche Attribut ist
  vollständig typisiert.
- Konkrete Typen sind `Any`, untypisierten Dictionaries und unnötigen `cast()`s
  vorzuziehen.
- Abstrakte Eingaben verwenden Typen aus `collections.abc`, zum Beispiel
  `Sequence`, `Mapping`, `Iterator` und `Callable`.
- Rückgabewerte und gespeicherter Zustand verwenden möglichst präzise Typen,
  bevorzugt unveränderliche Collections wie `tuple` und `frozenset`.
- `# type: ignore` braucht einen konkreten Fehlercode und einen erkennbaren Grund.
  In Tests ist es zulässig, um absichtlich ungültige Eingaben zu prüfen.

## Design

- Öffentliche APIs bleiben klein, explizit und schwer falsch zu benutzen.
- Framework-native Objekte wie `FastAPI`, `APIRouter` und Dishka-`Provider`
  werden komponiert und nicht unnötig abstrahiert oder nachgebaut.
- Deklarative Modelle sind standardmäßig unveränderlich, bevorzugt mit
  `@dataclass(frozen=True, slots=True)`.
- Veränderliche Eingaben werden validiert und vom Zustand des Aufrufers
  entkoppelt, bevor sie gespeichert werden.
- Es gibt keine prozessglobalen Registries, versteckten Singletons oder
  Installationen als Import-Nebeneffekt.
- Reihenfolge ist entweder bedeutungslos oder ausdrücklich definiert und
  getestet.
- Komfort darf keine Mehrdeutigkeit erzeugen. Keine impliziten Eingabeformen,
  automatischen Fallbacks oder Sonderfälle ohne klaren Anwendungsfall.
- Abwärtskompatibilität wird nicht vorsorglich erfunden. Kompatibilitätsschichten
  entstehen nur für eine tatsächlich veröffentlichte und unterstützte API.

## Funktionen und Klassen

- Eine Funktion erfüllt eine klar benennbare Aufgabe.
- Kontrollfluss bleibt flach. Guard Clauses sind tiefen Verschachtelungen
  vorzuziehen.
- Hilfsfunktionen werden erst extrahiert, wenn sie eine eigene Regel kapseln
  oder die Hauptlogik deutlich lesbarer machen.
- Klassen werden nur verwendet, wenn Identität, Zustand oder ein Protokoll sie
  rechtfertigen. Reine Daten bleiben Daten.
- Öffentliche Parameter sind keyword-only, wenn mehrere gleichartige Werte
  sonst leicht vertauscht werden können.
- Boolesche Optionen müssen positiv und eindeutig benannt sein.

## Validierung und Fehler

- Für Exception-Registrierung, Handler-Installation, Problem Details und die
  zugehörige OpenAPI-Integration ist das Schwesterprojekt `../fastapi_faults`
  die verbindliche Referenz und vorgesehene Integrationsbasis. Vor Änderungen
  in diesem Bereich wird dessen aktuelle Implementierung direkt geprüft.
- `fastapi-canon` implementiert keine konkurrierende Fault-Registry. Es
  komponiert die feature-lokalen Beiträge und übergibt die zusammengeführte
  Registry an `fastapi_faults`.
- Ungültige Konfiguration scheitert möglichst beim Erstellen oder Installieren,
  nicht erst beim ersten Request.
- Öffentliche Grenzen validieren Eingaben vollständig. Interne Funktionen dürfen
  auf bereits geprüfte Invarianten vertrauen.
- Erwartbare Konfigurationsfehler verwenden eine eigene, dokumentierte
  Exception-Klasse.
- Fehlermeldungen nennen den ungültigen Wert oder Pfad und die verletzte Regel.
- `assert` dient internen Invarianten und Tests, nicht der Validierung öffentlicher
  Eingaben.
- Exceptions werden nicht pauschal abgefangen. Falls ein sicherer Boundary-
  Fallback nötig ist, wird der ursprüngliche Fehler mit Kontext geloggt.

## Async und Lifespan

- `async` wird nur verwendet, wenn tatsächlich asynchrone Arbeit oder ein
  asynchrones Protokoll vorliegt.
- Ressourcen werden an derselben Abstraktionsgrenze freigegeben, an der sie
  erworben wurden.
- Startup und Shutdown bleiben symmetrisch. Teilweise erfolgreicher Startup muss
  bereits erworbene Ressourcen wieder freigeben.
- Cancellation wird nicht verschluckt.

## Kommentare und Dokumentation

- Kommentare erklären Gründe, Invarianten oder überraschende
  Framework-Einschränkungen. Sie wiederholen nicht den Code.
- Öffentliche Klassen und Funktionen erhalten kurze Docstrings, wenn Name und
  Signatur den Vertrag nicht vollständig erklären.
- Interne, offensichtliche Helfer brauchen keine Docstrings.
- Beispiele zeigen die kanonische API. Sie enthalten keine alternativen
  Schreibweisen, die nicht ebenfalls unterstützt und getestet werden.

## Tests

- Tests beschreiben beobachtbares Verhalten und Verträge, keine privaten
  Implementierungsschritte.
- Testnamen folgen dem Muster `test_<subject>_<behavior>`.
- Positive Fälle, Grenzfälle und relevante Fehlkonfigurationen werden geprüft.
- Reihenfolge, Idempotenz und Cleanup werden getestet, sobald sie Teil des
  Vertrags sind.
- Exakte Fehlermeldungen werden nur geprüft, wenn ihr Wortlaut öffentlicher
  Vertrag ist; ansonsten wird die relevante Diagnose mit `match=` geprüft.
- Mocks bleiben an Systemgrenzen. Kleine echte FastAPI-, Dishka- und Lifespan-
  Integrationen sind Mocks der Framework-Interna vorzuziehen.

## Qualitätschecks

Vor Abschluss einer Änderung müssen mindestens diese Checks erfolgreich sein:

```console
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv build
```

Warnungen werden nicht pauschal unterdrückt. Ausnahmen in der Tool-Konfiguration
brauchen einen engen Scope und einen dokumentierbaren Grund.
