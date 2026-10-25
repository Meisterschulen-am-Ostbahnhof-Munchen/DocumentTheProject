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
Standardmäßig wird es neben diesem Repository erwartet (`../IEC61499_FunctionBlock_SVG`),
sonst mit `--svg-tool` oder der Umgebungsvariable `IEC61499_SVG_TOOL` angeben.
Die TGL-Normschrift kommt aus dessen `tgl/`-Ordner.

## Bedienung

```bash
# Standard: ../4diac-SlurryTanker_AX-sample/.../4diacIDE-workspace/test
python document_project.py

# Eigenes Projekt (Projektordner mit .project oder Workspace mit genau einem Projekt)
python document_project.py C:\pfad\zum\4diacIDE-workspace\test -o Doku.pdf

# A3 und eigener Titel
python document_project.py C:\pfad\zum\projekt --paper A3 --title "Güllefass"
```

Unter Windows geht auch `generate_doc.bat` (per Doppelklick oder mit denselben Parametern).

| Parameter | Standard | Beschreibung |
|---|---|---|
| `project` | SlurryTanker-Beispiel | 4diac-Projektordner oder Workspace |
| `-o`, `--output` | `<Projekt>_Dokumentation.pdf` im Git-Repo des Projekts | Ausgabe-PDF |
| `--paper` | `A4` | `A4` oder `A3`; Hoch-/Querformat wird je Netzwerkblatt automatisch gewählt |
| `--title` | Systemname | Titel auf Deckblatt und Schriftfeld |
| `--lib` | aus `.project` | zusätzliche Typbibliothek (mehrfach möglich) |
| `--svg-tool` | `../IEC61499_FunctionBlock_SVG` | Pfad zum SVG-Konverter |

Die Typbibliotheken liest das Tool aus den `External Libraries`-Links in der `.project`-Datei
(`WORKSPACE_LOC/.lib/<lib>/typelib`).

## Lizenz

Eclipse Public License 2.0, siehe [LICENSE](LICENSE).
