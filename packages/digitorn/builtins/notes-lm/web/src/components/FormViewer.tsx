import { useCallback, useEffect, useMemo, useState } from "react";
import {
  requestToast,
  useChat,
  useWorkspaceFiles,
} from "@digitorn/preview-sdk";
import type {
  ChoiceField,
  CheckboxField,
  DateField,
  Field,
  FieldErrors,
  FormSchema,
  FormValues,
  GroupField,
  NumberField,
  SectionField,
  TextField,
  TextareaField,
} from "../lib/form-schema";
import {
  defaultValueFor,
  evaluateShowIf,
  isFormSchema,
  makeEmptyValues,
  normalizeOptions,
  validateForm,
} from "../lib/form-schema";

interface Props {
  path: string;
  /** Raw JSON content of forms/<slug>.json from the workspace. */
  source: string;
}

/**
 * Render a JSON form schema into a fully-interactive HTML form. The
 * agent writes ``forms/<slug>.json``, this component parses it,
 * renders the fields (with conditional visibility + sections +
 * repeating groups), validates on input + submit, and writes the
 * response to ``responses/<id>-<iso>.json`` on submit.
 */
export function FormViewer({ path, source }: Props) {
  const parsed = useMemo<{ schema: FormSchema; error: string | null }>(() => {
    try {
      const raw = JSON.parse(source) as unknown;
      if (!isFormSchema(raw)) {
        return { schema: { id: "", title: "", fields: [] }, error: "Not a valid form schema (missing id / title / fields)." };
      }
      return { schema: raw, error: null };
    } catch (err) {
      return {
        schema: { id: "", title: "", fields: [] },
        error: `Invalid JSON: ${err instanceof Error ? err.message : String(err)}`,
      };
    }
  }, [source]);

  const { schema, error: parseError } = parsed;
  const fs = useWorkspaceFiles();
  const chat = useChat();
  const [values, setValues] = useState<FormValues>(() => makeEmptyValues(schema.fields));
  const [errors, setErrors] = useState<FieldErrors>({});
  const [submitted, setSubmitted] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  // Re-seed values when the schema changes (e.g. agent rewrites it).
  // We KEEP the user's typed-in values when possible by merging on id,
  // so a refinement to the schema (added field, changed help text)
  // doesn't blow away in-progress input.
  useEffect(() => {
    setValues((prev) => {
      const fresh = makeEmptyValues(schema.fields);
      const merged: FormValues = { ...fresh };
      for (const k of Object.keys(fresh)) {
        if (k in prev) merged[k] = prev[k];
      }
      return merged;
    });
    setErrors({});
    setSubmitted(false);
  }, [schema]);

  const setFieldValue = useCallback((fieldId: string, value: unknown) => {
    setValues((prev) => ({ ...prev, [fieldId]: value }));
    // Clear the error on this field as the user types — re-validates
    // on submit. Live-validation is intentionally minimal so the user
    // isn't yelled at for half-typed input.
    setErrors((prev) => {
      if (!(fieldId in prev)) return prev;
      const { [fieldId]: _, ...rest } = prev;
      return rest;
    });
  }, []);

  const setGroupItemValue = useCallback(
    (groupId: string, index: number, fieldId: string, value: unknown) => {
      setValues((prev) => {
        const arr = ((prev[groupId] as FormValues[] | undefined) || []).slice();
        arr[index] = { ...(arr[index] || {}), [fieldId]: value };
        return { ...prev, [groupId]: arr };
      });
      setErrors((prev) => {
        const key = `${groupId}.${index}.${fieldId}`;
        if (!(key in prev)) return prev;
        const { [key]: _, ...rest } = prev;
        return rest;
      });
    },
    [],
  );

  const addGroupItem = useCallback((group: GroupField) => {
    setValues((prev) => {
      const arr = ((prev[group.id] as FormValues[] | undefined) || []).slice();
      arr.push(makeEmptyValues(group.fields));
      return { ...prev, [group.id]: arr };
    });
  }, []);

  const removeGroupItem = useCallback((groupId: string, index: number) => {
    setValues((prev) => {
      const arr = ((prev[groupId] as FormValues[] | undefined) || []).slice();
      arr.splice(index, 1);
      return { ...prev, [groupId]: arr };
    });
  }, []);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitted(true);
    const errs = validateForm(schema, values);
    setErrors(errs);
    if (Object.keys(errs).length > 0) {
      requestToast("Please fix the highlighted fields", "error");
      // Scroll to the first error.
      requestAnimationFrame(() => {
        const el = document.querySelector<HTMLElement>(".form-field-error");
        el?.scrollIntoView({ behavior: "smooth", block: "center" });
      });
      return;
    }

    setSubmitting(true);
    try {
      const now = new Date();
      const iso = now.toISOString().replace(/[:.]/g, "-");
      const slug = schema.id || extractSlug(path);
      const responsePath = `responses/${slug}-${iso}.json`;
      const payload = {
        form_id: schema.id,
        form_path: path,
        submitted_at: now.toISOString(),
        values,
      };
      await fs.writeFile(
        responsePath,
        JSON.stringify(payload, null, 2),
        { autoApprove: true },
      );
      requestToast("Submitted", "success");
      // Kick the agent immediately - same path as the chat composer.
      // The submitted file is on disk, the agent reads it via WsRead
      // and replies in the SAME session without the user having to
      // type a follow-up message. The synthetic user message is
      // visible in the chat history so the conversation stays
      // self-explanatory.
      await chat.send(
        `J'ai soumis le formulaire \`${path}\` (réponse: ` +
        `\`${responsePath}\`). Lis-la avec \`WsRead("${responsePath}")\` ` +
        `et analyse mes réponses.`,
      );
    } catch (err) {
      requestToast(
        `Submit failed: ${err instanceof Error ? err.message : String(err)}`,
        "error",
      );
    } finally {
      setSubmitting(false);
    }
  }

  if (parseError) {
    return (
      <div className="form-parse-error">
        <strong>Couldn't parse this form:</strong>
        <p>{parseError}</p>
        <p className="hint">
          Open the file in the source viewer (toggle off the form
          renderer) to inspect the raw JSON.
        </p>
      </div>
    );
  }

  return (
    <form className="form-viewer" onSubmit={handleSubmit} noValidate>
      <div className="form-header">
        <h2>{schema.title}</h2>
        {schema.description && (
          <p className="form-description">{schema.description}</p>
        )}
      </div>

      <div className="form-fields">
        {schema.fields.map((f) => (
          <FieldRenderer
            key={f.id}
            field={f}
            values={values}
            scope={values}
            errors={errors}
            errorPrefix=""
            submitted={submitted}
            setFieldValue={setFieldValue}
            setGroupItemValue={setGroupItemValue}
            addGroupItem={addGroupItem}
            removeGroupItem={removeGroupItem}
          />
        ))}
      </div>

      <div className="form-actions">
        <button type="submit" className="form-submit" disabled={submitting}>
          {submitting ? "Submitting..." : (schema.submit_label || "Submit")}
        </button>
      </div>
    </form>
  );
}

// ── Field dispatch ───────────────────────────────────────────────────

interface FieldRendererProps {
  field: Field;
  values: FormValues;
  /** For show_if lookup. For group instance fields, this is the
   *  instance's own values, NOT the top-level form values. */
  scope: FormValues;
  errors: FieldErrors;
  /** Path prefix into errors map. Empty for top-level, ``contacts.0.``
   *  for a field inside the first instance of the ``contacts`` group. */
  errorPrefix: string;
  submitted: boolean;
  setFieldValue: (fieldId: string, value: unknown) => void;
  setGroupItemValue: (
    groupId: string, index: number, fieldId: string, value: unknown,
  ) => void;
  addGroupItem: (group: GroupField) => void;
  removeGroupItem: (groupId: string, index: number) => void;
  /** When set, ``setFieldValue`` is rerouted through ``setGroupItemValue``. */
  groupContext?: { groupId: string; index: number };
}

function FieldRenderer(props: FieldRendererProps) {
  const { field, scope } = props;
  if (!evaluateShowIf(field.show_if, scope)) return null;

  switch (field.type) {
    case "section":
      return <SectionRenderer {...props} field={field} />;
    case "group":
      return <GroupRenderer {...props} field={field} />;
    case "text": case "email": case "url": case "tel":
      return <TextFieldRenderer {...props} field={field} />;
    case "textarea":
      return <TextareaFieldRenderer {...props} field={field} />;
    case "number": case "range":
      return <NumberFieldRenderer {...props} field={field} />;
    case "date": case "datetime-local": case "time":
      return <DateFieldRenderer {...props} field={field} />;
    case "select": case "multiselect": case "radio":
      return <ChoiceFieldRenderer {...props} field={field} />;
    case "checkbox":
      return <CheckboxFieldRenderer {...props} field={field} />;
  }
}

// ── Section + Group ─────────────────────────────────────────────────

function SectionRenderer(p: FieldRendererProps & { field: SectionField }) {
  return (
    <fieldset className="form-section">
      {p.field.title && <legend>{p.field.title}</legend>}
      {p.field.description && (
        <p className="section-description">{p.field.description}</p>
      )}
      {p.field.fields.map((f) => (
        <FieldRenderer key={f.id} {...p} field={f} />
      ))}
    </fieldset>
  );
}

function GroupRenderer(p: FieldRendererProps & { field: GroupField }) {
  const arr = (p.values[p.field.id] as FormValues[] | undefined) || [];
  const groupErr = p.errors[`${p.errorPrefix}${p.field.id}`];
  const max = p.field.max;
  const canAdd = max === undefined || arr.length < max;

  return (
    <div className="form-group">
      <div className="form-group-header">
        {p.field.title && <h3>{p.field.title}</h3>}
        {p.field.description && (
          <p className="section-description">{p.field.description}</p>
        )}
      </div>
      {groupErr && p.submitted && (
        <div className="form-field-error">{groupErr}</div>
      )}
      {arr.length === 0 && (
        <div className="form-group-empty">
          No entries yet. Use "{p.field.add_label || "Add"}" below.
        </div>
      )}
      {arr.map((instance, idx) => (
        <div key={idx} className="form-group-item">
          <div className="form-group-item-header">
            <span className="form-group-item-index">#{idx + 1}</span>
            <button
              type="button"
              className="form-group-remove"
              onClick={() => p.removeGroupItem(p.field.id, idx)}
              aria-label={`Remove entry ${idx + 1}`}
              title="Remove this entry"
            >
              Remove
            </button>
          </div>
          {p.field.fields.map((sub) => (
            <FieldRenderer
              key={sub.id}
              {...p}
              field={sub}
              scope={instance}
              errorPrefix={`${p.errorPrefix}${p.field.id}.${idx}.`}
              groupContext={{ groupId: p.field.id, index: idx }}
            />
          ))}
        </div>
      ))}
      <button
        type="button"
        className="form-group-add"
        onClick={() => p.addGroupItem(p.field)}
        disabled={!canAdd}
      >
        + {p.field.add_label || "Add"}
      </button>
    </div>
  );
}

// ── Common field shell ──────────────────────────────────────────────

function FieldShell({
  field,
  error,
  children,
}: {
  field: Field;
  error?: string;
  children: React.ReactNode;
}) {
  const required = "required" in field && field.required;
  return (
    <div className={`form-field ${error ? "has-error" : ""}`}>
      {field.label && (
        <label className="form-label" htmlFor={`f-${field.id}`}>
          {field.label}
          {required && <span className="form-required" aria-hidden> *</span>}
        </label>
      )}
      {children}
      {field.help && !error && (
        <div className="form-help">{field.help}</div>
      )}
      {error && <div className="form-field-error">{error}</div>}
    </div>
  );
}

function _resolveValue(p: FieldRendererProps, fieldId: string): unknown {
  if (p.groupContext) {
    return (p.values[p.groupContext.groupId] as FormValues[] | undefined)
      ?.[p.groupContext.index]?.[fieldId];
  }
  return p.scope[fieldId];
}

function _onChange(p: FieldRendererProps, fieldId: string, value: unknown) {
  if (p.groupContext) {
    p.setGroupItemValue(p.groupContext.groupId, p.groupContext.index, fieldId, value);
  } else {
    p.setFieldValue(fieldId, value);
  }
}

// ── Per-type renderers ──────────────────────────────────────────────

function TextFieldRenderer(p: FieldRendererProps & { field: TextField }) {
  const err = p.submitted ? p.errors[`${p.errorPrefix}${p.field.id}`] : undefined;
  const v = _resolveValue(p, p.field.id);
  return (
    <FieldShell field={p.field} error={err}>
      <input
        id={`f-${p.field.id}`}
        type={p.field.type}
        className="form-input"
        value={(v as string) || ""}
        placeholder={p.field.placeholder}
        maxLength={p.field.maxLength}
        pattern={p.field.pattern}
        onChange={(e) => _onChange(p, p.field.id, e.target.value)}
      />
    </FieldShell>
  );
}

function TextareaFieldRenderer(p: FieldRendererProps & { field: TextareaField }) {
  const err = p.submitted ? p.errors[`${p.errorPrefix}${p.field.id}`] : undefined;
  const v = _resolveValue(p, p.field.id);
  const len = typeof v === "string" ? v.length : 0;
  return (
    <FieldShell field={p.field} error={err}>
      <textarea
        id={`f-${p.field.id}`}
        className="form-textarea"
        value={(v as string) || ""}
        placeholder={p.field.placeholder}
        rows={p.field.rows || 4}
        maxLength={p.field.maxLength}
        onChange={(e) => _onChange(p, p.field.id, e.target.value)}
      />
      {p.field.maxLength && (
        <div className="form-counter">{len} / {p.field.maxLength}</div>
      )}
    </FieldShell>
  );
}

function NumberFieldRenderer(p: FieldRendererProps & { field: NumberField }) {
  const err = p.submitted ? p.errors[`${p.errorPrefix}${p.field.id}`] : undefined;
  const v = _resolveValue(p, p.field.id);
  const display = v === "" || v === undefined || v === null ? "" : String(v);
  if (p.field.type === "range") {
    const num = typeof v === "number" ? v : Number(v);
    const fallback = p.field.min ?? 0;
    return (
      <FieldShell field={p.field} error={err}>
        <div className="form-range-wrap">
          <input
            id={`f-${p.field.id}`}
            type="range"
            className="form-range"
            value={Number.isFinite(num) ? num : fallback}
            min={p.field.min}
            max={p.field.max}
            step={p.field.step}
            onChange={(e) => _onChange(p, p.field.id, Number(e.target.value))}
          />
          <span className="form-range-value">
            {Number.isFinite(num) ? num : fallback}
          </span>
        </div>
      </FieldShell>
    );
  }
  return (
    <FieldShell field={p.field} error={err}>
      <input
        id={`f-${p.field.id}`}
        type="number"
        className="form-input"
        value={display}
        placeholder={p.field.placeholder}
        min={p.field.min}
        max={p.field.max}
        step={p.field.step}
        onChange={(e) => _onChange(p, p.field.id, e.target.value === "" ? "" : Number(e.target.value))}
      />
    </FieldShell>
  );
}

function DateFieldRenderer(p: FieldRendererProps & { field: DateField }) {
  const err = p.submitted ? p.errors[`${p.errorPrefix}${p.field.id}`] : undefined;
  const v = _resolveValue(p, p.field.id);
  return (
    <FieldShell field={p.field} error={err}>
      <input
        id={`f-${p.field.id}`}
        type={p.field.type}
        className="form-input"
        value={(v as string) || ""}
        min={p.field.min}
        max={p.field.max}
        onChange={(e) => _onChange(p, p.field.id, e.target.value)}
      />
    </FieldShell>
  );
}

function ChoiceFieldRenderer(p: FieldRendererProps & { field: ChoiceField }) {
  const err = p.submitted ? p.errors[`${p.errorPrefix}${p.field.id}`] : undefined;
  const v = _resolveValue(p, p.field.id);
  const options = normalizeOptions(p.field.options);

  if (p.field.type === "select") {
    return (
      <FieldShell field={p.field} error={err}>
        <select
          id={`f-${p.field.id}`}
          className="form-input"
          value={(v as string) || ""}
          onChange={(e) => _onChange(p, p.field.id, e.target.value)}
        >
          <option value="">{p.field.placeholder || "Select..."}</option>
          {options.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
      </FieldShell>
    );
  }

  if (p.field.type === "radio") {
    return (
      <FieldShell field={p.field} error={err}>
        <div className="form-radio-group" role="radiogroup">
          {options.map((o) => (
            <label key={o.value} className="form-radio-item">
              <input
                type="radio"
                name={`f-${p.field.id}`}
                value={o.value}
                checked={(v as string) === o.value}
                onChange={() => _onChange(p, p.field.id, o.value)}
              />
              <span>{o.label}</span>
            </label>
          ))}
        </div>
      </FieldShell>
    );
  }

  // multiselect — checkboxes
  const arr = Array.isArray(v) ? (v as string[]) : [];
  return (
    <FieldShell field={p.field} error={err}>
      <div className="form-checkbox-group">
        {options.map((o) => {
          const checked = arr.includes(o.value);
          return (
            <label key={o.value} className="form-checkbox-item">
              <input
                type="checkbox"
                checked={checked}
                onChange={() => {
                  const next = checked
                    ? arr.filter((x) => x !== o.value)
                    : [...arr, o.value];
                  _onChange(p, p.field.id, next);
                }}
              />
              <span>{o.label}</span>
            </label>
          );
        })}
      </div>
    </FieldShell>
  );
}

function CheckboxFieldRenderer(p: FieldRendererProps & { field: CheckboxField }) {
  const err = p.submitted ? p.errors[`${p.errorPrefix}${p.field.id}`] : undefined;
  const v = _resolveValue(p, p.field.id);
  return (
    <div className={`form-field form-field-inline ${err ? "has-error" : ""}`}>
      <label className="form-checkbox-single">
        <input
          id={`f-${p.field.id}`}
          type="checkbox"
          checked={Boolean(v)}
          onChange={(e) => _onChange(p, p.field.id, e.target.checked)}
        />
        <span>{p.field.label}</span>
      </label>
      {p.field.help && !err && (
        <div className="form-help">{p.field.help}</div>
      )}
      {err && <div className="form-field-error">{err}</div>}
    </div>
  );
}

// Suppress "default unused" warnings — they are exported via types only.
void defaultValueFor;

function extractSlug(path: string): string {
  const base = path.includes("/") ? path.slice(path.lastIndexOf("/") + 1) : path;
  return base.replace(/\.json$/i, "");
}
