# Zielbild

> Status: Entwurf zur fachlichen Validierung. Dieses Dokument beschreibt nur
> das angestrebte Zielbild. Konkrete APIs, Installationsalgorithmen und
> Testanforderungen werden erst nach der Freigabe dieses Zielbilds spezifiziert.

## Ausgangslage

Eine größere FastAPI-Anwendung besteht selten nur aus Routern. Ein fachliches
Feature wie `project`, `browser_tunnel` oder `usability_study` bringt typischerweise
mehrere technische Beiträge zur Anwendung mit:

- einen oder mehrere FastAPI-Router,
- eigene Dishka-Provider für seine Abhängigkeiten,
- fachliche Fehler und deren HTTP-Darstellung,
- Startup- und Shutdown-Logik,
- optional eine konfigurationsabhängige Aktivierung.

Ohne eine gemeinsame Komposition werden diese Beiträge an verschiedenen Stellen
des Application Bootstraps registriert. Beim Hinzufügen oder Entfernen eines
Features müssen dann Router, Provider, Fehlerbehandlung und Lifespan separat
angepasst werden. Die fachliche Einheit ist im Code vorhanden, wird am
Composition Root aber wieder auseinandergerissen. Dadurch kann ein Feature nur
teilweise installiert werden, die Installationsreihenfolge bleibt implizit und
Konfigurationsfehler zeigen sich unter Umständen erst beim ersten Request.

## Produktvision

`fastapi-canon` soll eine kleine, opinionated Kompositionsbibliothek für
feature-orientierte FastAPI-Anwendungen werden. Die Bibliothek behandelt einen
vertikalen Feature Slice als atomare Einheit der Anwendungskomposition.

Ein Feature beschreibt deklarativ alles, was es zur FastAPI-Anwendung beiträgt.
Der Composition Root wählt lediglich die gewünschten Features aus und installiert
sie gemeinsam. Die resultierende App-Erstellung soll absichtlich langweilig,
explizit und auf einen Blick verständlich sein:

```python
from fastapi import FastAPI

from backend.features.browser_tunnel import browser_tunnel_feature
from backend.features.project import project_feature
from backend.features.usability_study import usability_study_feature
from backend.shared.features import install_features


app = FastAPI()

install_features(
    app,
    project_feature,
    browser_tunnel_feature,
    usability_study_feature,
)
```

Die Liste der Features soll die fachliche Gestalt der API zeigen. Leserinnen und
Leser sollen den Bootstrap verstehen können, ohne die internen Module der
Features zu kennen. Ein neues Feature wird im Regelfall durch genau einen
zusätzlichen Eintrag in dieser Liste Teil der Anwendung.

## Das Feature als Kompositionseinheit

Das zentrale Modell ist ein unveränderlicher Feature-Deskriptor. Der folgende
Entwurf beschreibt die gewünschte Form, ohne die endgültige öffentliche API
vorwegzunehmen:

```python
@dataclass(frozen=True)
class Feature:
    routers: tuple[APIRouter, ...] = ()
    providers: tuple[Provider, ...] = ()
    exception_handlers: tuple[ExceptionHandler, ...] = ()
    lifespan: Lifespan | None = None
```

Jeder Beitrag ist optional. Damit kann dieselbe Abstraktion kleine Features mit
nur einem Router ebenso darstellen wie größere Features mit eigener
Infrastruktur und Lebenszykluslogik. Ein Feature muss keine künstlichen
Platzhalter definieren, wenn es eine bestimmte Art von Beitrag nicht benötigt.

Der Deskriptor ist eine Beschreibung und kein zweiter Anwendungscontainer. Er
soll weder Requests verarbeiten noch Abhängigkeiten selbst auflösen. Seine
Aufgabe ist es, zusammengehörige, framework-native Bausteine zu bündeln und
dem gemeinsamen Installer zu übergeben.

## Gewünschter Zuschnitt eines Feature Slice

Ein Feature Slice besitzt seine fachlichen Modelle, Anwendungslogik,
Schnittstellen und Integrationsdefinitionen möglichst in einem gemeinsamen
Modulbaum. Dazu gehören insbesondere der Router, die Dishka-Provider, die
Fehlerdefinitionen beziehungsweise Handler und die Lifespan-Logik des Features.

Die Feature-Instanz bildet die schmale öffentliche Integrationsfläche dieses
Modulbaums. Andere Features und der Composition Root sollen die internen
Einzelteile nicht separat importieren müssen. Dadurch bleibt sichtbar, welche
Funktionalität zusammengehört, und das Feature kann als Einheit hinzugefügt,
entfernt und getestet werden.

Die Kapselung soll fachliche Abhängigkeiten nicht verstecken. Wenn ein Feature
ein anderes Feature voraussetzt oder eine gemeinsam bereitgestellte
Abstraktion benötigt, muss diese Beziehung bei der Komposition eindeutig und
prüfbar bleiben. Das Ziel ist lokale Kohäsion, nicht implizite Magie.

## Gemeinsame Installation

`install_features(...)` ist die zentrale Integrationsoperation. Sie nimmt eine
FastAPI-Anwendung und eine explizit geordnete Liste von Features entgegen. Aus
diesen Definitionen entsteht eine konsistente Anwendung mit:

- allen aktiven Routern,
- einem gemeinsam nutzbaren Dishka-Container aus den Feature-Providern,
- einer zusammenhängenden Fehlerbehandlung,
- einem kombinierten Lifespan für Startup und Shutdown.

Die Installation soll deterministisch sein. Dieselbe geordnete Feature-Liste
und dieselbe Konfiguration sollen dieselbe Anwendungsstruktur erzeugen. Die
Reihenfolge darf dort relevant sein, wo FastAPI oder die Lebenszyklen eine
Reihenfolge besitzen; sie darf jedoch keine zufälligen oder importabhängigen
Ergebnisse erzeugen.

Fehlerhafte Kombinationen sollen während der App-Erstellung scheitern. Dazu
gehören beispielsweise unvereinbare Fehlerdefinitionen, doppeldeutige
Registrierungen, nicht erfüllbare Abhängigkeiten oder ungültige
Lifespan-Kompositionen. Eine Anwendung, deren Features nicht konsistent
zusammenpassen, soll nicht scheinbar erfolgreich starten und erst unter Last
fehlschlagen.

## FastAPI-Integration

Die Bibliothek soll bestehende FastAPI-Konzepte komponieren, statt sie durch
eigene Parallelabstraktionen zu ersetzen. Feature-Router bleiben normale
`APIRouter`-Instanzen. Die fertig komponierte Anwendung bleibt eine normale
`FastAPI`-Instanz und soll weiterhin mit dem FastAPI-Ökosystem, OpenAPI,
Testclients und ASGI-Servern funktionieren.

Globale Anwendungsentscheidungen bleiben am Composition Root. Dazu zählen etwa
Anwendungsmetadaten, globale Middleware, deployment-spezifische Konfiguration
und Policies, die absichtlich für die gesamte API gelten. Ein Feature soll nur
die Beiträge besitzen, die fachlich zu ihm gehören.

## Dishka-Integration

Jedes Feature kann seine eigenen Dishka-Provider colocated definieren und über
seinen Deskriptor bereitstellen. Die Anwendung soll daraus eine gemeinsame
Dependency-Injection-Umgebung erhalten, sodass Abhängigkeiten featureübergreifend
auflösbar sind, sofern sie ausdrücklich bereitgestellt werden.

Die Komposition soll Dishka als Dependency-Injection-System respektieren. Die
Feature-Abstraktion ist kein eigener Service Locator und führt keine zweite
Auflösungslogik ein. Provider-Lebenszyklen und Scopes müssen mit dem kombinierten
FastAPI-Lifespan und dem Request-Lebenszyklus kohärent bleiben.

## Fehlerbehandlung

Fachliche Fehler sollen beim Feature definiert werden, das sie besitzt. Die
Anwendung führt diese lokalen Definitionen zu einem konsistenten HTTP-Fehlervertrag
zusammen.

Als Referenz dient das Kompositionsmodell von `fastapi_faults`: Features können
ihre Fehler lokal beschreiben; am Composition Root werden die Definitionen
deterministisch zusammengeführt und einmal für die Anwendung installiert. Dabei
sollen Laufzeitantworten und OpenAPI-Dokumentation aus derselben Definition
entstehen. Kollisionen, etwa bei Exception-Klassen, stabilen Fehlercodes,
Problem-Type-URIs oder Schemanamen, sollen als Konfigurationsfehler auffallen.

Das Zielbild schließt sowohl fachliche Fehlerverträge nach diesem Modell als
auch bewusst definierte FastAPI-/Starlette-Exception-Handler ein. Die spätere
API-Spezifikation muss klären, ob `exception_handlers` unmittelbar Handler,
Fehler-Registries oder einen allgemeineren installierbaren Fehlerbeitrag
enthält. Für das Zielbild ist entscheidend, dass ein Feature seine
Fehlerbehandlung vollständig mitliefert und die Anwendung sie genau einmal
koordiniert installiert.

## Lifespan-Komposition

Ein Feature kann Ressourcen besitzen, die beim Start der Anwendung aufgebaut
und beim Beenden wieder freigegeben werden müssen, zum Beispiel Clients,
Worker, Tunnel oder Verbindungen. Diese Logik soll beim Feature liegen und als
Teil seiner Installation berücksichtigt werden.

Mehrere Feature-Lifespans müssen sich zu einem einzigen FastAPI-kompatiblen
Lifespan verbinden lassen. Startup und Shutdown bilden dabei einen gemeinsamen,
geordneten Lebenszyklus. Wenn ein späterer Startup-Schritt scheitert, dürfen
bereits gestartete Ressourcen nicht ohne Aufräumen zurückbleiben. Die
Komposition soll außerdem mit dem Lebenszyklus des Dishka-Containers
zusammenpassen.

## Feature Toggles

Ein Feature soll optional von Konfiguration oder Deployment abhängig aktiviert
werden können. Ein deaktiviertes Feature darf keine Teilinstallation
hinterlassen: Seine Router, Provider, Fehlerbehandlung und Lifespan-Beiträge
werden gemeinsam ein- oder ausgeschlossen.

Die Entscheidung über aktive Features gehört in die Kompositionsphase. Damit
bleibt die resultierende Anwendungsstruktur für die gesamte Laufzeit konsistent
und OpenAPI beschreibt dieselbe Oberfläche, die tatsächlich installiert ist.
Die konkrete Form der Toggles ist noch offen; sie kann durch Auswahl der
Feature-Liste oder durch eine deklarative Aktivierungsbedingung am Feature
entstehen.

## Leitprinzipien

Das angestrebte Design folgt diesen Prinzipien:

1. **Feature-lokale Kohäsion:** Alles, was ausschließlich zu einem fachlichen
   Feature gehört, liegt bei diesem Feature.
2. **Explizite Komposition:** Die Anwendung zeigt am Composition Root, welche
   Features sie enthält.
3. **Unveränderliche Deklaration:** Feature-Definitionen sind Werte, die sicher
   importiert, wiederverwendet und zusammengesetzt werden können.
4. **Framework-native Bausteine:** FastAPI, Starlette und Dishka bleiben als
   solche erkennbar und direkt nutzbar.
5. **Atomare Aktivierung:** Alle Beiträge eines Features werden gemeinsam
   installiert oder gemeinsam ausgelassen.
6. **Deterministisches Verhalten:** Reihenfolge und Ergebnis der Komposition
   sind nachvollziehbar und reproduzierbar.
7. **Frühes Scheitern:** Widersprüchliche oder unvollständige Konfiguration wird
   beim Erstellen beziehungsweise Starten der Anwendung sichtbar.
8. **Keine versteckten globalen Registries:** Die wirksame Anwendung ergibt sich
   aus den an `install_features(...)` übergebenen Werten, nicht aus
   Import-Nebeneffekten.
9. **Eine Integrationsstelle:** Der Composition Root koordiniert Router,
   Dependency Injection, Fehlerbehandlung und Lifespan gemeinsam.
10. **Geringe Zeremonie:** Ein übliches Feature benötigt wenig Bibliothekscode
    und bleibt für FastAPI-Entwicklerinnen und -Entwickler sofort lesbar.

## Erfolgskriterien für das Zielbild

Das Ziel ist erreicht, wenn die Bibliothek folgende Entwicklungserfahrung
ermöglicht:

- Ein Feature kann mit allen zugehörigen Integrationsbeiträgen in seinem eigenen
  Modulbaum entwickelt werden.
- Der öffentliche Integrationspunkt eines Feature Slice ist eine einzige,
  unveränderliche Feature-Definition.
- Das Hinzufügen eines Features zur Anwendung ist im Normalfall ein zusätzlicher
  Eintrag bei `install_features(...)`.
- Das Entfernen oder Deaktivieren eines Features entfernt sämtliche Beiträge
  dieses Features konsistent.
- Router, Dishka-Provider, Fehlerbehandlung und Lifespan können gemeinsam
  installiert werden, ohne dass der Bootstrap jedes Subsystem separat kennen
  muss.
- Die Installation mehrerer Features bleibt verständlich, deterministisch und
  unabhängig von versteckten Import-Nebeneffekten.
- Fehler in der Feature-Komposition werden vor dem regulären Request-Betrieb mit
  verständlichen Diagnosen gemeldet.
- Feature-lokale Fehlerverträge können nach dem Vorbild von `fastapi_faults`
  zusammengeführt werden, ohne dass Laufzeitverhalten und OpenAPI auseinanderlaufen.
- Kleine Features bleiben klein; ungenutzte Integrationsarten erzeugen keine
  Pflichtkonfiguration.
- Die resultierende Anwendung verhält sich weiterhin wie eine gewöhnliche
  FastAPI-/ASGI-Anwendung und bleibt mit vorhandenen Werkzeugen testbar und
  betreibbar.

Dieses Zielbild bildet die Grundlage für die nachfolgende API- und
Verhaltensspezifikation. Insbesondere die genaue Repräsentation von
Exception-Beiträgen, Lifespan-Abhängigkeiten, Installationsreihenfolge,
Idempotenz und Feature-Toggles ist damit noch nicht festgelegt.
