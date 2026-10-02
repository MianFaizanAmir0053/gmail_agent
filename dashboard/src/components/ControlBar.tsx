import { SwitchHeader } from "@/components/SwitchHeader";
import { switchesOrNull } from "@/lib/proposals";

/**
 * The owner's switches, on every page (M17, D5 and D6): Pause or Resume, and
 * a banner while the agent is paused or its new work has stopped.
 *
 * It sits in the layout, so a failed read must not take the page down with
 * it: the header then offers Pause alone, and says it could not read the
 * rest. Next's own signals, such as the redirect to sign in, still pass
 * through (`switchesOrNull`). The client part takes each fresh read, and
 * reads again after a navigation (`SwitchHeader`).
 */
export async function ControlBar() {
  return <SwitchHeader initial={await switchesOrNull()} />;
}
