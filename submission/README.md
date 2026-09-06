# Submission materials

| File | What it is |
| --- | --- |
| `devpost.json` | The Devpost project copy: tagline, description, built-with, disclosure. |
| `video-script.txt` | The ≤5 minute demo script, with a pre-recording checklist. |
| `architecture.png` | The gallery image. Devpost requires an uploaded image; a README link does not count. |
| `architecture-write-path.mmd`, `architecture-read-path.mmd` | Mermaid sources for the two bands in that image. |

## Regenerating the architecture image

```sh
npm install -g @mermaid-js/mermaid-cli
mmdc -i submission/architecture-write-path.mmd -o /tmp/write.png -b white -s 2
mmdc -i submission/architecture-read-path.mmd  -o /tmp/read.png  -b white -s 2
```

The two renders are then stacked under a title band. The bands are kept as separate
Mermaid files on purpose: Mermaid lays a single graph containing both paths out either
as a very tall column or a very wide strip, and neither is legible as a gallery
thumbnail.
