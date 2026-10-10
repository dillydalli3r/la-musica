import { Loader2 } from "lucide-react";

/** Held until the config answers, in the SETUP frame rather than the shell's
 *  skeleton: on a first run the next thing on screen is the wizard, and the
 *  shell (sidebar, player, library) painting for a beat first is the library
 *  UI flashing through setup. */
export default function SetupLoading() {
  return (
    <div className="safe-shell min-h-full bg-bg text-zinc-100 flex flex-col items-center justify-center">
      <Loader2 className="h-5 w-5 animate-spin text-zinc-500" />
    </div>
  );
}