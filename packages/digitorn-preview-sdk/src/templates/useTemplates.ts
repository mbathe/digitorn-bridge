/**
 * Selection state for ``<TemplateGallery>`` + ``<TemplateModal>``.
 *
 * Tiny hook — returns the input list plus a controlled
 * ``{selected, pick, dismiss}`` triple. The consuming app feeds
 * ``list`` to the gallery, ``selected`` to the modal, ``pick`` /
 * ``dismiss`` to both. The hook does NOT decide what happens on
 * confirm — that's the app's ``onConfirm`` callback on the modal.
 */

import { useCallback, useState } from "react";

import type { Template } from "./types.js";

export interface UseTemplatesApi {
  /** The input templates, returned as-is for convenience. */
  list: Template[];
  /** Currently-open template, or ``null`` when nothing is selected. */
  selected: Template | null;
  /** Open the modal for the given template. */
  pick: (template: Template) => void;
  /** Close the modal. */
  dismiss: () => void;
  /**
   * Find a template by id and pick it. Useful for deep-linking
   * (e.g. ``?template=portfolio-minimal`` in the URL).
   */
  pickById: (id: string) => boolean;
}

export function useTemplates(list: Template[]): UseTemplatesApi {
  const [selected, setSelected] = useState<Template | null>(null);

  const pick = useCallback((template: Template) => {
    setSelected(template);
  }, []);

  const dismiss = useCallback(() => {
    setSelected(null);
  }, []);

  const pickById = useCallback(
    (id: string) => {
      const found = list.find((t) => t.id === id);
      if (!found) return false;
      setSelected(found);
      return true;
    },
    [list],
  );

  return { list, selected, pick, dismiss, pickById };
}
