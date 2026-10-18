import { useState, useMemo } from "react";
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
// props.title: string
// props.template_id: string

export default function HitlForm() {
  const initialFields = useMemo(
    () => (props.fields || []).map((f) => ({ ...f })),
    []
  );

  const [fields, setFields] = useState(initialFields);
  const [submitted, setSubmitted] = useState(false);

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

            {field.type === "select" ? (
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
