# DocumentTheProject
make a PDF from a 4diac Project

Erzeugt aus einem Eclipse 4diac™-Projekt eine PDF-Dokumentation im Stil eines Schaltplans.

## Aufbau des PDFs

1. **Deckblatt**: Systemname, Identification, Version, Ressourcen, Systembeschreibung
2. **Inhaltsverzeichnis**: alle Blätter mit Hierarchiepfad, klickbar
3. **Ressource** (z. B. `FORTE_PC.EMB_RES`): Netzwerk der Ressource mit den zugeordneten
   (gemappten) Applikationselementen, dazu ein Übersichtsblatt mit Gerät, Parametern,
   Mapping, Kommunikation und Instanzliste
4. **Rekursiv in die Tiefe**: Jeder Projekttyp wird **einmal** dokumentiert. Typen mit Netzwerk
   (SubApp, Composite-FB) bekommen ein Netzwerkblatt; jeder Typ bekommt ein Schnittstellenblatt
   (Schnittstellengrafik, Ereignisse, Daten, Adapter, Instanzen, Gruppen; bei Basis-/Simple-FBs
   ECC und Algorithmen)
5. **Nicht verwendete Projekttypen und Datentypen** des Projekts
6. **Anhang**: verwendete Bibliothekstypen mit Verwendungsstellen, nicht auflösbare Typen
   (mit Grund), globale Konstanten (`.gcf`), Bibliotheksabhängigkeiten (`MANIFEST.MF`)

Jedes Blatt hat einen **Zeichnungsrahmen** (Heftrand links, Feldeinteilung `1…n` / `A…x`)
und ein **Schriftfeld** mit Projekt, Blattinhalt, Typ und **„Blatt n von m“**.

### Verweise

- Neben jeder Instanz im Netzwerk steht ein Verweis auf das Blatt ihres Typs, z. B. `-> S9`.
  Mehrere Instanzen desselben Typs verweisen alle auf dasselbe Blatt.
- Das Typ-Blatt listet unter „Verwendet in“ alle Verwendungsstellen als `Blatt/Feld`,
  z. B. `S8/E3 (Klappen), S8/B4 (Verteiler)`.
- Alle Verweise (auch jede einzelne Verwendungsstelle), das Inhaltsverzeichnis (inklusive
  Fortsetzungsblätter) und die PDF-Lesezeichen sind klickbar.

## Voraussetzungen

```bash
pip install -r requirements.txt
```

Die Grafiken erzeugt [IEC61499_FunctionBlock_SVG](https://github.com/Meisterschulen-am-Ostbahnhof-Munchen/IEC61499_FunctionBlock_SVG).
Der Pfad wird mit `--svg-tool` oder der Umgebungsvariable `IEC61499_SVG_TOOL` angegeben.
Die TGL-Normschrift kommt aus dessen `tgl/`-Ordner.

In `document_project.py` steht kein Pfad. Alle Pfade stehen oben in `generate_doc.bat`
und werden als Parameter übergeben.

## Bedienung

Unter Windows: `generate_doc.bat` per Doppelklick. Ohne Parameter dokumentiert sie das
SlurryTanker-Beispiel mit Bedienoberfläche, die Pfade stehen oben in der Datei (`C:\git\ms\...`).
Mit Parametern reicht sie diese an `document_project.py` weiter und ergänzt nur `--svg-tool`.

```bash
# Projektordner mit .project oder Workspace mit genau einem Projekt
python document_project.py C:\pfad\zum\4diacIDE-workspace\test --svg-tool C:\pfad\zu\IEC61499_FunctionBlock_SVG -o Doku.pdf

# A3 und eigener Titel
python document_project.py C:\pfad\zum\projekt --svg-tool ... --paper A3 --title "Güllefass"
```

| Parameter | Standard | Beschreibung |
|---|---|---|
| `project` | (Pflicht) | 4diac-Projektordner oder Workspace |
| `--svg-tool` | `IEC61499_SVG_TOOL` | Pfad zum SVG-Konverter (Pflicht) |
| `-o`, `--output` | `<Projekt>_Dokumentation.pdf` im Git-Repo des Projekts | Ausgabe-PDF |
| `--paper` | `A4` | `A4` oder `A3`; Hoch-/Querformat wird je Netzwerkblatt automatisch gewählt |
| `--title` | Systemname | Titel auf Deckblatt und Schriftfeld |
| `--lib` | aus `.project` | zusätzliche Typbibliothek (mehrfach möglich) |
| `--vt-pool` | – | ISO-Designer-Poolordner (`.jop`, `.jvi`, `Output/<Pool>.iop`) |
| `--pool-scripts` | – | Ordner mit `list_mask_objects.py` (`scripts_central`), nötig mit `--vt-pool` |
| `--vt-render` | – | `vt-render.exe`; ohne nur Tabellen, keine Bilder |
| `--vt-softkeys` | größte Soft-Key-Maske | Soft-Keys des Terminals |
| `--vt-mask-size` | `ISO_MASK_SIZE` aus `.iop.h` | Datenmaskengröße in Pixel |

Die Typbibliotheken liest das Tool aus den `External Libraries`-Links in der `.project`-Datei
(`WORKSPACE_LOC/.lib/<lib>/typelib`).

## Bedienoberfläche (ISOBUS-VT)

Mit `--vt-pool` kommt vor dem Anhang das Kapitel **Bedienoberfläche** dazu:

- **Übersicht**: Pooldateien, SHA-256, Maskengröße, Soft-Key-Anordnung, Working-Set-Symbol,
  Liste der Masken, Auxiliary-Funktionen mit ihren 4diac-Verwendungen und die Objekte, die der
  Renderer nicht zeichnet
- **je Maske ein Bildblatt**: Datenmasken mit ihrer Soft-Key-Maske daneben, wie im Terminal,
  Alarm- und Soft-Key-Masken einzeln
- **je Maske eine Objekttabelle**: alle Objekte der Maske als Baum (ohne CProxy), mit
  `Blatt/Feld` auf dem Bildblatt und „Verwendet in (4diac)“
- **Working Set und Auxiliary-Funktionen**: je ein Blatt mit dem Objektbaum. Der VT zeichnet
  Auxiliary-Funktionen nicht, deshalb zeigt die Bildspalte dort die Bitmap aus dem
  ISO-Designer-Projekt (`img/`)

Querverweise in beide Richtungen: Nennt ein Parameter einer 4diac-Instanz ein Objekt des Pools
(`u16DataMaskId = DataMask_1000`, `IN1 = SoftKeyMask_PAGE_1`, an ID-Eingängen von
ISOBUS-Bausteinen wie `u16ObjId` auch die nackte Objekt-ID), dann zeigt die Instanztabelle `VT S<n>`
(je VT-Blatt ein Link), und die Objekttabelle listet die Instanz.

Passt `Output/<Pool>.iop.h` nicht mehr zur `.jop` (Objekt umbenannt oder neu nummeriert, Pool nicht
neu übersetzt), zeichnet das Tool keine Bilder und nennt den Grund auf dem Übersichtsblatt.

Woher die Teile kommen:

- **Objektbaum, Namen, Klassen**: `list_mask_objects.py` aus `scripts_central`
  (z. B. `4diac_training1/Ventilsteuerung/scripts_central`), liest `.jop` und `.jvi`
- **Bilder**: `vt-render` aus [AgIsoVirtualTerminal PR 224](https://github.com/Open-Agriculture/AgIsoVirtualTerminal/pull/224)
  (Branch `feat/object-pool-screenshots`, noch Entwurf). Er zeichnet die übersetzte `.iop` mit den
  Komponenten des VT, Text in DejaVu Sans Mono statt der Schrift des Terminals.

`vt-render` bauen (Visual Studio Build Tools mit CMake und Ninja), in der „x64 Native Tools“-Konsole:

```bat
cd C:\git\ms\AgIsoVirtualTerminal
cmake -S . -B build-renderer -G Ninja -DCMAKE_BUILD_TYPE=Release -DAGISOVT_BUILD_POOL_RENDERER=ON
cmake --build build-renderer --target VtRender -j 4
```

Ergebnis: `build-renderer\VtRender_artefacts\Release\vt-render.exe`.

## Lizenz

Eclipse Public License 2.0, siehe [LICENSE](LICENSE).
