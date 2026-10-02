import { unstable_rethrow } from "next/navigation";

import { SwitchButton } from "@/components/SwitchButton";
import { switches } from "@/lib/proposals";
import { banners, type Switches } from "@/lib/switches";

/**
 * The owner's switches, on every page (M17, D5 and D6): Pause or Resume, and
 * a banner while the agent is paused or its spending is capped.
 *
 * It sits in the layout, so a failed read must not take the page down with
 * it: the header goes without the switches until the next render. Next's own
 * signals, such as the redirect to sign in, still pass through.
 */
export async function ControlBar() {
  let state: Switches;
  try {
    state = await switches();
  } catch (error) {
    unstable_rethrow(error);
    console.error("switches not read:", error instanceof Error ? error.name : "unknown error");
    return null;
  }
  const shown = banners(state);

  return (
    <>
      <SwitchButton paused={state.paused} />
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
