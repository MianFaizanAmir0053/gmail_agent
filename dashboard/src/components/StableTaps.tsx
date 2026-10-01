"use client";

import { createContext, useContext, useLayoutEffect, useRef, useState, type ReactNode } from "react";

/** How long decision buttons ignore taps after the cards move. */
const HOLD_MS = 1000;

const Settling = createContext(false);

/** True for a moment after the cards moved: decision buttons ignore taps. */
export function useSettling(): boolean {
  return useContext(Settling);
}

/**
 * Holds the decision buttons still for a moment whenever the open cards move.
 *
 * The page re-reads every few seconds while a decision is applied. A card
 * that settles leaves, a new one arrives at the top, an edited one comes back
 * at a new revision, and every card below shifts. A tap already on its way to
 * one card's Cancel could then land on another card's Confirm, which Fly
 * would accept, because that card is pending at the revision its form
 * carries. So for a second after `layoutKey` changes, Confirm, Send edit and
 * Cancel are disabled: a tap in that second does nothing, rather than the
 * wrong thing.
 *
 * Set before the browser paints the moved cards, so no frame shows them
 * moved but still live.
 */
export function StableTaps({ layoutKey, children }: { layoutKey: string; children: ReactNode }) {
  const seen = useRef(layoutKey);
  const [settling, setSettling] = useState(false);

  useLayoutEffect(() => {
    if (seen.current === layoutKey) return;
    seen.current = layoutKey;
    setSettling(true);
    const timer = setTimeout(() => setSettling(false), HOLD_MS);
    return () => clearTimeout(timer);
  }, [layoutKey]);

  return <Settling.Provider value={settling}>{children}</Settling.Provider>;
}
