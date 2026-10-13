import { openExternal } from "../lib/externalLinks";

/** One numbered line of a connection how-to. `url` (with its `label`) is the
 *  ONE click that line needs — the page the credential is made on — opened
 *  through the app's external-link helper, so the desktop shell hands it to
 *  the OS browser instead of dropping it (see lib/externalLinks.ts). */
interface ConnectStep {
  text: string;
  label?: string;
  url?: string;
}

/** The two flows, in their own words. Both are the same shape: make the
 *  credential on the service's page (one click), paste it, then Connect →
 *  authorize → paste back what the service shows. Kept here, beside the one
 *  component that draws them, so the Last.fm panel and the MusicBrainz panel
 *  cannot drift from each other — they are the same surface on Settings and on
 *  the setup wizard. */
const GUIDES = {
  lastfm: [
    {
      text: "Create a Last.fm API account — that page shows this app's API key and its shared secret side by side.",
      label: "Open last.fm/api/account/create",
      url: "https://www.last.fm/api/account/create",
    },
    {
      text: "Paste the API key into the Last.fm row on the Sources step (later: Settings → Sources) and the shared secret into the “Last.fm shared secret” field.",
    },
    {
      text: "Press Connect to open Last.fm's authorization page, allow the app there, then paste the token Last.fm shows into the box and press Finish.",
    },
  ] as ConnectStep[],
  musicbrainz: [
    {
      text: "Register an application at musicbrainz.org/account/applications — an installed (public) client whose redirect URI is urn:ietf:wg:oauth:2.0:oob.",
      label: "Open musicbrainz.org/account/applications",
      url: "https://musicbrainz.org/account/applications",
    },
    {
      text: "Paste its Client ID and Client secret into the “MusicBrainz OAuth2 client” fields.",
    },
    {
      text: "Press Connect, authorize the app in the browser that opens, then paste the code MusicBrainz shows into the box and press Finish.",
    },
  ] as ConnectStep[],
} satisfies Record<string, ConnectStep[]>;

/** The numbered how-to a connection panel draws above its own controls: the
 *  steps are the FLOW's, not one screen's, so every surface that draws the
 *  panel (Settings, the MusicBrainz pages, the setup wizard) reads them
 *  identically. Each line that names a page has one button that opens it. */
export default function ConnectGuide({
  service,
  className = "",
}: {
  service: keyof typeof GUIDES;
  className?: string;
}) {
  return (
    <ol className={`list-decimal list-inside space-y-0.5 text-[11px] text-zinc-400 leading-relaxed ${className}`}>
      {GUIDES[service].map((step, i) => (
        <li key={i}>
          {step.text}
          {step.url && (
            <>
              {" "}
              <button
                type="button"
                className="underline decoration-dotted text-accent-soft hover:text-accent tap"
                title={step.url}
                onClick={() => openExternal(step.url!)}
              >
                {step.label ?? step.url}
              </button>
            </>
          )}
        </li>
      ))}
    </ol>
  );
}