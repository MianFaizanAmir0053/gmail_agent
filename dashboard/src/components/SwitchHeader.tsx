"use client";

import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { SwitchButton } from "@/components/SwitchButton";
import { banners, parseSwitches, type Switches } from "@/lib/switches";

/**
 * The header's switch and banners (M17, D5 and D6).
 *
 * The root layout renders this once, and a navigation does not render the
 * layout again. So after each navigation it reads the switches again, from
 * `/api/switches`, and a pause made on another device or the command line
 * shows on the next page. A failed read keeps what it shows.
 *
 * `initial` is null when the server could not read the switches. Pause is
 * still offered: pausing a paused agent changes nothing.
 */
export function SwitchHeader({ initial }: { initial: Switches | null }) {
  const [current, setCurrent] = useState(initial);
  const pathname = usePathname();
  const shownFor = useRef(pathname);
  const renders = useRef(0);

  // Every server render wins: after Pause or Resume, or a refresh, the layout
  // renders again with a fresh read, which is always a new object.
  useEffect(() => {
    renders.current += 1;
    setCurrent(initial);
  }, [initial]);

  useEffect(() => {
    if (shownFor.current === pathname) return;
    shownFor.current = pathname;
    let live = true;
    // A read that a server render overtook is older than what it shows.
    const asOf = renders.current;
    fetch("/api/switches", { cache: "no-store" })
      .then((response) => (response.ok ? response.json() : null))
      .then((body: unknown) => {
        const read = parseSwitches(body);
        if (live && read !== null && renders.current === asOf) setCurrent(read);
      })
      .catch(() => {
        // Keep what is shown; the next navigation tries again.
      });
    return () => {
      live = false;
    };
  }, [pathname]);

  if (current === null) {
    return (
      <>
        <SwitchButton paused={false} />
        <div className="banners">
          <p className="banner warn" role="status">
            The switches could not be read. Pause still works.
          </p>
        </div>
      </>
    );
  }

  const shown = banners(current);
  return (
    <>
      {/* Keyed by the state, so a half-asked Resume never outlives it. */}
      <SwitchButton key={String(current.paused)} paused={current.paused} />
      {shown.length > 0 && (
        <div className="banners">
          {shown.map((banner) => (
            <p key={banner.text} className={`banner ${banner.tone}`} role="status">
              {banner.text}
            </p>
          ))}
        </div>
      )}
    </>
  );
}
