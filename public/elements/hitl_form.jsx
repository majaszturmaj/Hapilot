import { useState, useMemo, useEffect } from "react";
import { Card, CardHeader, CardTitle, CardContent, CardFooter } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

// props.fields: [{id, label, type, value, options?, required?}]
//   type "signature" (src/signatures.py): options [{value, title, hint, image}] shown as picture cards; add_url, has_scan
// props.title: string
// props.template_id: string

// One signature option as a picture: the scan, or the empty place for a handwritten signature.
function SignatureCard({ option, selected, onSelect }) {
  const border = selected ? "2px solid hsl(var(--primary))" : "1px solid hsl(var(--border))";
  return (
    <button type="button" onClick={onSelect} aria-pressed={selected}
      style={{ flex: "1 1 220px", maxWidth: 320, textAlign: "left", border, borderRadius: 12, padding: 10, cursor: "pointer",
               background: selected ? "hsl(var(--primary) / 0.08)" : "transparent", color: "inherit",
               boxShadow: selected ? "0 0 0 3px hsl(var(--primary) / 0.18)" : "none" }}>
      <div style={{ background: "#fff", borderRadius: 6, padding: "12px 16px 8px", display: "flex", flexDirection: "column",
                    alignItems: "center", boxShadow: "0 1px 4px rgba(0,0,0,.15)" }}>
        <div style={{ height: 56, width: "100%", display: "flex", alignItems: "flex-end", justifyContent: "center" }}>
          {option.image
            ? <img src={option.image} alt={option.title} style={{ maxHeight: 54, maxWidth: "100%", objectFit: "contain" }} />
            : <span style={{ color: "#9aa", fontSize: 11, marginBottom: 6, border: "1px dashed #ccd", borderRadius: 6, padding: "14px 22px" }}>miejsce na podpis odręczny</span>}
        </div>
      </div>
      <div style={{ marginTop: 8, display: "flex", alignItems: "center", gap: 8 }}>
        <span style={{ width: 16, height: 16, borderRadius: "50%", flex: "none",
                       border: selected ? "5px solid hsl(var(--primary))" : "2px solid hsl(var(--muted-foreground))" }} />
        <span style={{ fontWeight: 600, fontSize: 13 }}>{option.title}</span>
      </div>
      <div style={{ fontSize: 12, opacity: 0.7, marginTop: 2, marginLeft: 24 }}>{option.hint}</div>
    </button>
  );
}

export default function HitlForm() {
  const initialFields = useMemo(
    () => (props.fields || []).map((f) => ({ ...f })),
    []
  );

  const [fields, setFields] = useState(initialFields);
  const [submitted, setSubmitted] = useState(false);

  // A scan added in another tab (✍️ Mój podpis) appears here when the user comes back to the form.
  useEffect(() => {
    if (!initialFields.some((f) => f.type === "signature")) return undefined;
    const refresh = async () => {
      try {
        const res = await fetch("/hapilot/admin/api/signature", { credentials: "same-origin" });
        if (!res.ok) return;
        const sig = await res.json();
        setFields((prev) => prev.map((f) => {
          if (f.type !== "signature") return f;
          const others = (f.options || []).filter((o) => o.image === null);
          const options = sig.present
            ? [...others, { value: "Skan mojego podpisu", title: "Mój skan", hint: "Twój podpis wstawiony w dokument", image: sig.src }]
            : others;
          const value = options.some((o) => o.value === f.value) ? f.value : others[0].value;
          return { ...f, options, value, has_scan: Boolean(sig.present) };
        }));
      } catch (e) { /* keep the options the form came with */ }
    };
    window.addEventListener("focus", refresh);
    return () => window.removeEventListener("focus", refresh);
  }, []);

  const updateField = (index, newValue) => {
    setFields((prev) => {
      const copy = [...prev];
      copy[index] = { ...copy[index], value: newValue };
      return copy;
    });
  };

  const handleSubmit = () => {
    setSubmitted(true);
    const result = { submitted: true };
    fields.forEach((f) => {
      result[f.id] = f.value || "";
    });
    submitElement(result);
  };

  const handleCancel = () => {
    setSubmitted(false);
    cancelElement();
  };

  if (submitted) {
    return (
      <Card className="w-full max-w-2xl border-green-500/30 bg-green-500/5">
        <CardContent className="pt-4 pb-4 text-center text-green-400">
          ✅ Formularz zatwierdzony — generuję dokument...
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="w-full max-w-2xl">
      <CardHeader className="pb-3">
        <CardTitle className="text-base">
          📋 {props.title || "Parametry dokumentu"}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {fields.map((field, idx) => (
          <div key={field.id} className="space-y-1">
            <Label htmlFor={field.id} className="text-sm font-medium">
              {field.label || field.id}
              {field.required && <span className="text-red-400 ml-1">*</span>}
            </Label>

            {field.type === "signature" ? (
              <div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 12 }}>
                  {(field.options || []).map((opt) => (
                    <SignatureCard key={opt.value} option={opt} selected={field.value === opt.value}
                      onSelect={() => updateField(idx, opt.value)} />
                  ))}
                </div>
                <div style={{ fontSize: 12, opacity: 0.75, marginTop: 8 }}>
                  {field.has_scan ? "Skan zmienisz w " : "Nie masz jeszcze skanu podpisu — dodasz go w "}
                  <a href={field.add_url} target="_blank" rel="noopener" style={{ textDecoration: "underline" }}>✍️ Mój podpis</a>
                  {field.has_scan ? "." : " (nowa karta) — po powrocie Twój skan pojawi się tutaj."}
                </div>
              </div>
            ) : field.type === "select" ? (
              <Select
                value={field.value || ""}
                onValueChange={(val) => updateField(idx, val)}
              >
                <SelectTrigger id={field.id}>
                  <SelectValue placeholder="Wybierz..." />
                </SelectTrigger>
                <SelectContent>
                  {(field.options || []).map((opt) => (
                    <SelectItem key={opt} value={opt}>
                      {opt}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            ) : field.type === "textarea" ? (
              <Textarea
                id={field.id}
                value={field.value || ""}
                onChange={(e) => updateField(idx, e.target.value)}
                rows={4}
                className="resize-y text-sm"
              />
            ) : (
              <Input
                id={field.id}
                type={field.type === "date" ? "date" : "text"}
                value={field.value || ""}
                onChange={(e) => updateField(idx, e.target.value)}
                className="text-sm"
              />
            )}
          </div>
        ))}
      </CardContent>
      <CardFooter className="flex justify-end gap-2 pt-2">
        <Button variant="outline" size="sm" onClick={handleCancel}>
          ❌ Anuluj
        </Button>
        <Button size="sm" onClick={handleSubmit}>
          ✅ Generuj PDF
        </Button>
      </CardFooter>
    </Card>
  );
}
