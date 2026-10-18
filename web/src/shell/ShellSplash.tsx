import { useI18n } from "../lib/i18n";

/** The desktop shell's boot frame: brand + one sentence, and no form of any
 *  kind. It stands where the sign-in screen used to stand while a status
 *  query was still in flight (or had failed once) — a form there asked the
 *  user for a password to a server that, on this machine, never wants one —
 *  and it is also the body of the backend chooser's frame while a LOCAL shell
 *  waits for its server. Either way: the brand and one sentence, nothing to
 *  fill in. Inside the chooser's frame the rail stays put, so the window never
 *  goes blank between the shell's own splash and the server's first answer:
 *  what is on screen the whole time is a real element, and the sentence is the
 *  only thing that changes.
 *
 *  It renders no button of its own: this is the SAME component the sign-in
 *  page draws, and a "connect to another server" there would be an action for
 *  a state (address unknown) that screen is not in. */
export default function ShellSplash() {
  const { t } = useI18n();
  return (
    <div className="safe-shell min-h-full bg-bg text-zinc-100 flex flex-col items-center justify-center">
      <div className="flex flex-col items-center gap-3 px-6 text-center">
        <img
          src="/icon.png"
          alt="la musica"
          className="h-14 w-14 rounded-xl object-cover ring-1 ring-border shadow-lg"
        />
        <p className="text-sm text-zinc-300" aria-live="polite">
          {t("auth.starting")}
        </p>
      </div>
    </div>
  );
}