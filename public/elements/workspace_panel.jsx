import { useState, useEffect, useMemo } from "react";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

// Right-hand panel "Źródła i pliki" (data: src/ui_panel.py, src/source_view.py).
// props.turns: [{id, question, sources: [...], files: [...]}]
// props.active: id of the turn to show

const MARK = "rgba(250, 204, 21, 0.38)";
const MARK_ACTIVE = "rgba(250, 204, 21, 0.7)";
const OUTLINE = "hsl(var(--primary))";
const MUTED = "hsl(var(--muted-foreground))";
const BORDER = "1px solid hsl(var(--border))";

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} kB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function where(chunk) {
  if (chunk.page) return `str. ${chunk.page}`;
  if (chunk.sheet) return `arkusz ${chunk.sheet}`;
  return "";
}

function scrollToChunk(no) {
  const el = document.getElementById(`hp-chunk-${no}`);
  if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
}

// ------------------------------------------------------------------ document views

function PdfPages({ pages, active }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {pages.map((page) => {
        const firstOfChunk = {};
        return (
          <div key={page.n}>
            <div style={{ fontSize: 12, color: MUTED, marginBottom: 4 }}>Strona {page.n}</div>
            <div style={{ position: "relative", width: "100%", paddingTop: `${(page.ratio || 1.414) * 100}%`,
                          border: BORDER, background: "white" }}>
              <img src={page.url} loading="lazy" alt={`Strona ${page.n}`}
                   style={{ position: "absolute", inset: 0, width: "100%", height: "100%" }} />
              {(page.boxes || []).map((b, i) => {
                const id = firstOfChunk[b.chunk] ? undefined : `hp-chunk-${b.chunk}`;
                firstOfChunk[b.chunk] = true;
                return (
                  <div key={i} id={id} title={`Fragment ${b.chunk + 1}`}
                       style={{ position: "absolute", left: `${b.x * 100}%`, top: `${b.y * 100}%`,
                                width: `${b.w * 100}%`, height: `${b.h * 100}%`, mixBlendMode: "multiply",
                                background: b.chunk === active ? MARK_ACTIVE : MARK, borderRadius: 2 }} />
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// Split text into plain and marked pieces; overlapping marks are clipped.
function pieces(text, marks) {
  const out = [];
  let cursor = 0;
  for (const [start, end, no] of marks) {
    const s = Math.max(start, cursor);
    if (end <= s) continue;
    if (s > cursor) out.push({ text: text.slice(cursor, s) });
    out.push({ text: text.slice(s, end), no });
    cursor = end;
  }
  if (cursor < text.length) out.push({ text: text.slice(cursor) });
  return out;
}

function TextSections({ sections, active }) {
  const seen = {};
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {sections.map((section, si) => (
        <div key={si}>
          {section.label && <div style={{ fontSize: 12, color: MUTED, marginBottom: 4 }}>{section.label}</div>}
          <div style={{ whiteSpace: "pre-wrap", fontSize: 13, lineHeight: 1.55, border: BORDER, borderRadius: 6,
                        padding: 12, overflowWrap: "anywhere" }}>
            {pieces(section.text, section.marks).map((p, i) => {
              if (p.no === undefined) return <span key={i}>{p.text}</span>;
              const id = seen[p.no] ? undefined : `hp-chunk-${p.no}`;
              seen[p.no] = true;
              return (
                <mark key={i} id={id} title={`Fragment ${p.no + 1}`}
                      style={{ background: p.no === active ? MARK_ACTIVE : MARK, color: "inherit", borderRadius: 2 }}>
                  {p.text}
                </mark>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}

function TableSections({ sections, active }) {
  const seen = {};
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {sections.map((section, si) => {
        let offset = 0;
        const rows = section.text.split("\n").map((line) => {
          const start = offset;
          offset += line.length + 1;
          const hit = section.marks.find(([s, e]) => s < start + line.length && e > start);
          return { cells: line.split(" | "), no: hit ? hit[2] : undefined };
        });
        return (
          <div key={si}>
            {section.label && <div style={{ fontSize: 12, color: MUTED, marginBottom: 4 }}>{section.label}</div>}
            <div style={{ overflowX: "auto", border: BORDER, borderRadius: 6 }}>
              <table style={{ borderCollapse: "collapse", fontSize: 12, width: "100%" }}>
                <tbody>
                  {rows.map((row, ri) => {
                    const id = row.no !== undefined && !seen[row.no] ? `hp-chunk-${row.no}` : undefined;
                    if (row.no !== undefined) seen[row.no] = true;
                    return (
                      <tr key={ri} id={id}
                          style={{ background: row.no === undefined ? "transparent" : row.no === active ? MARK_ACTIVE : MARK,
                                   fontWeight: ri === 0 ? 600 : 400 }}>
                        {row.cells.map((c, ci) => (
                          <td key={ci} style={{ borderBottom: BORDER, padding: "4px 6px", whiteSpace: "nowrap" }}>{c}</td>
                        ))}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function DocumentView({ doc, onBack }) {
  const [active, setActive] = useState(doc.chunks.length ? 0 : -1);
  useEffect(() => {
    if (doc.chunks.length) {
      const t = setTimeout(() => scrollToChunk(0), 300);
      return () => clearTimeout(t);
    }
  }, [doc.source]);
  const pick = (no) => {
    setActive(no);
    scrollToChunk(no);
  };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <Button variant="outline" size="sm" onClick={onBack}>← Lista</Button>
        <div style={{ fontWeight: 600, overflowWrap: "anywhere" }}>{doc.name}</div>
      </div>
      <div style={{ fontSize: 12, color: MUTED, overflowWrap: "anywhere" }}>{doc.source}</div>
      {doc.chunks.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {doc.chunks.map((c) => (
            <Button key={c.no} size="sm" variant={c.no === active ? "default" : "outline"} onClick={() => pick(c.no)}
                    title={c.text.slice(0, 200)}>
              Fragment {c.no + 1}{where(c) ? ` · ${where(c)}` : ""}
            </Button>
          ))}
        </div>
      )}
      {doc.kind === "pdf" && <PdfPages pages={doc.pages} active={active} />}
      {doc.kind === "text" && <TextSections sections={doc.sections} active={active} />}
      {doc.kind === "table" && <TableSections sections={doc.sections} active={active} />}
      {doc.kind === "missing" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <div style={{ fontSize: 13, color: MUTED }}>
            ⚠️ Nie mogę otworzyć tego dokumentu{doc.error ? ` (${doc.error})` : ""}. Poniżej fragmenty użyte w odpowiedzi.
          </div>
          {doc.chunks.map((c) => (
            <div key={c.no} id={`hp-chunk-${c.no}`}
                 style={{ whiteSpace: "pre-wrap", fontSize: 13, background: MARK, borderRadius: 6, padding: 10 }}>
              {c.text}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ lists

function SourceList({ sources, onOpen }) {
  if (!sources.length) return <div style={{ fontSize: 13, color: MUTED }}>Ta odpowiedź nie korzystała z dokumentów.</div>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {sources.map((doc, i) => (
        <button key={doc.source} onClick={() => onOpen(i)}
                style={{ textAlign: "left", border: BORDER, borderRadius: 8, padding: 10, cursor: "pointer",
                         background: "transparent", color: "inherit", borderLeft: doc.cited ? `3px solid ${OUTLINE}` : BORDER }}>
          <div style={{ fontWeight: 600, overflowWrap: "anywhere" }}>📄 {doc.name}</div>
          <div style={{ fontSize: 12, color: MUTED, overflowWrap: "anywhere", marginTop: 2 }}>{doc.source}</div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 6 }}>
            {doc.cited ? <Badge>cytowany w odpowiedzi</Badge> : <Badge variant="secondary">użyty, nie wymieniony</Badge>}
            {doc.chunks.length > 0 && <Badge variant="outline">fragmenty: {doc.chunks.length}</Badge>}
            {doc.read_by_code && <Badge variant="outline">odczytany przez kod</Badge>}
          </div>
        </button>
      ))}
    </div>
  );
}

function DiffView({ diff }) {
  return (
    <pre style={{ fontSize: 12, border: BORDER, borderRadius: 6, padding: 8, overflowX: "auto", margin: 0 }}>
      {diff.split("\n").map((line, i) => {
        const color = line.startsWith("+") && !line.startsWith("+++") ? "rgba(34,197,94,0.18)"
          : line.startsWith("-") && !line.startsWith("---") ? "rgba(239,68,68,0.18)" : "transparent";
        return <div key={i} style={{ background: color }}>{line || " "}</div>;
      })}
    </pre>
  );
}

function FileItem({ file }) {
  const [view, setView] = useState(null);
  const canDiff = file.status === "modified" && file.diff;
  return (
    <div style={{ border: BORDER, borderRadius: 8, padding: 10, display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <div style={{ fontWeight: 600, overflowWrap: "anywhere", flex: "1 1 auto" }}>{file.name}</div>
        {file.status === "created" ? <Badge>✨ nowy</Badge> : <Badge variant="secondary">✏️ zmieniony</Badge>}
        <span style={{ fontSize: 12, color: MUTED }}>{formatSize(file.size)}</span>
      </div>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        <a href={file.url} download={file.name}
           style={{ fontSize: 13, border: BORDER, borderRadius: 6, padding: "4px 10px", color: "inherit",
                    textDecoration: "none" }}>⬇️ Pobierz</a>
        {(file.preview || file.pages) && (
          <Button size="sm" variant={view === "preview" ? "default" : "outline"}
                  onClick={() => setView(view === "preview" ? null : "preview")}>Podgląd</Button>
        )}
        {canDiff && (
          <Button size="sm" variant={view === "diff" ? "default" : "outline"}
                  onClick={() => setView(view === "diff" ? null : "diff")}>Zmiany</Button>
        )}
      </div>
      {view === "preview" && file.pages && <PdfPages pages={file.pages} active={-1} />}
      {view === "preview" && !file.pages && file.preview && (
        <pre style={{ fontSize: 12, whiteSpace: "pre-wrap", border: BORDER, borderRadius: 6, padding: 8, margin: 0,
                      maxHeight: 480, overflow: "auto", overflowWrap: "anywhere" }}>{file.preview}</pre>
      )}
      {view === "diff" && canDiff && <DiffView diff={file.diff} />}
    </div>
  );
}

function FileList({ files }) {
  if (!files.length) return <div style={{ fontSize: 13, color: MUTED }}>Ta odpowiedź nie utworzyła ani nie zmieniła plików.</div>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {files.map((f) => <FileItem key={f.name} file={f} />)}
    </div>
  );
}

// ------------------------------------------------------------------ panel

export default function WorkspacePanel() {
  const turns = props.turns || [];
  const [turnId, setTurnId] = useState(props.active);
  const turn = useMemo(() => turns.find((t) => t.id === turnId) || turns[turns.length - 1], [turnId]);
  const [tab, setTab] = useState(turn && !turn.sources.length && turn.files.length ? "files" : "sources");
  const [openDoc, setOpenDoc] = useState(turn && turn.sources.length === 1 ? 0 : null);

  useEffect(() => {
    setTurnId(props.active);
  }, [props.active]);
  useEffect(() => {
    if (turn) window.hapilotPanelTurn = turn.id; // public/panel_tab.js reopens the panel on this turn
    setOpenDoc(turn && turn.sources.length === 1 ? 0 : null);
    if (turn) setTab(!turn.sources.length && turn.files.length ? "files" : "sources");
  }, [turn && turn.id]);

  if (!turn) return <div style={{ fontSize: 13, color: MUTED }}>Brak danych.</div>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10, padding: 4 }}>
      {turns.length > 1 && (
        <select value={turn.id} onChange={(e) => setTurnId(Number(e.target.value))}
                style={{ border: BORDER, borderRadius: 6, padding: "6px 8px", fontSize: 13,
                         background: "hsl(var(--background))", color: "inherit" }}>
          {turns.map((t) => <option key={t.id} value={t.id}>{t.question || `Odpowiedź ${t.id}`}</option>)}
        </select>
      )}
      {turns.length <= 1 && <div style={{ fontSize: 13, color: MUTED }}>„{turn.question}”</div>}
      <Tabs value={tab} onValueChange={setTab}>
        <TabsList style={{ width: "100%" }}>
          <TabsTrigger value="sources" style={{ flex: 1 }}>Źródła ({turn.sources.length})</TabsTrigger>
          <TabsTrigger value="files" style={{ flex: 1 }}>Pliki ({turn.files.length})</TabsTrigger>
        </TabsList>
        <TabsContent value="sources">
          {openDoc === null
            ? <SourceList sources={turn.sources} onOpen={setOpenDoc} />
            : <DocumentView doc={turn.sources[openDoc]} onBack={() => setOpenDoc(null)} />}
        </TabsContent>
        <TabsContent value="files">
          <FileList files={turn.files} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
