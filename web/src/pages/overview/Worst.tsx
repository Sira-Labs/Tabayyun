import { Link } from "@tanstack/react-router";
import { scoreTone } from "../../components/Badges";
import { formatLocalTime, formatNumber } from "../../format";
import { metricLabel, scoreText, seriesFindingsSearch, shownWords, type Overview } from "../../overview";
import { mutedClass } from "../suites/parts";

const cell = "px-3 py-2 align-top";

/** A score coloured like the score tiles, or a dash. */
function Score({ value }: { value: number | null }) {
  return <span className={`tabular-nums ${value === null ? mutedClass : scoreTone(value)}`}>{scoreText(value)}</span>;
}

/** "Worst series": the series ranked by their lowest value, with links to each series and to
 * its unresolved findings in the inbox. */
export function WorstSeries({ worst }: { worst: Overview["worst"] }) {
  const shown = shownWords(worst.items.length, worst.n);
  return (
    <section aria-labelledby="overview-worst-heading" className="space-y-2">
      <h2 id="overview-worst-heading" className="text-base font-semibold">
        Worst series
      </h2>
      {worst.items.length === 0 ? (
        <p className={`text-sm ${mutedClass}`}>No scored series.</p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
          <table aria-label="Worst series, lowest value first" className="w-full text-left text-sm">
            <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-600 dark:border-slate-800 dark:text-slate-400">
              <tr>
                <th scope="col" className={cell}>Series</th>
                <th scope="col" className={cell}>Asset</th>
                <th scope="col" className={cell}>Lowest</th>
                <th scope="col" className={`${cell} text-right`}>Value</th>
                <th scope="col" className={`${cell} text-right`}>Overall</th>
                <th scope="col" className={`${cell} text-right`}>Unresolved findings</th>
              </tr>
            </thead>
            <tbody>
              {worst.items.map((item) => (
                <tr key={item.series_id} className="border-b border-slate-100 last:border-0 dark:border-slate-800">
                  <td className={cell}>
                    <Link
                      to="/series/$seriesId"
                      params={{ seriesId: item.series_id }}
                      title={`Scored ${formatLocalTime(item.computed_at)}`}
                      className="font-medium underline-offset-2 hover:underline"
                    >
                      {item.name}
                    </Link>
                  </td>
                  <td className={`${cell} break-all font-mono text-xs`}>{item.asset_path ?? <span className={mutedClass}>—</span>}</td>
                  <td className={cell}>{metricLabel(item.metric)}</td>
                  <td className={`${cell} text-right`}>
                    <Score value={item.value} />
                  </td>
                  <td className={`${cell} text-right`}>
                    <Score value={item.overall} />
                  </td>
                  <td className={`${cell} text-right tabular-nums`}>
                    {item.open_findings > 0 ? (
                      <Link
                        to="/findings"
                        search={seriesFindingsSearch(item.series_id)}
                        aria-label={`${formatNumber(item.open_findings)} unresolved findings of ${item.name}`}
                        className="font-medium text-sky-800 underline underline-offset-2 dark:text-sky-300"
                      >
                        {formatNumber(item.open_findings)}
                      </Link>
                    ) : (
                      <span className={mutedClass}>0</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {shown && <p className={`text-sm ${mutedClass}`}>{shown}</p>}
    </section>
  );
}
