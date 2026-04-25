import { DEFAULT_VAULT_PATH } from "./config.js";
import { statusAndAudit } from "./sovereign.js";
import { auditTail, ensureVault, vaultFirstLearn, writeVaultPage } from "./vault.js";

async function main() {
  const [command, ...args] = process.argv.slice(2);

  if (!command || command === "help") {
    console.log("Usage: node dist/cli.js <status|ensure-vault|audit-tail|learn|write> ...");
    return;
  }

  if (command === "ensure-vault") {
    console.log(JSON.stringify(await ensureVault(DEFAULT_VAULT_PATH), null, 2));
    return;
  }

  if (command === "status") {
    console.log(JSON.stringify(await statusAndAudit(DEFAULT_VAULT_PATH), null, 2));
    return;
  }

  if (command === "audit-tail") {
    const limit = Number(args[0] ?? "20");
    console.log((await auditTail(DEFAULT_VAULT_PATH, limit)).text);
    return;
  }

  if (command === "learn") {
    const [title, ...contentParts] = args;
    if (!title || contentParts.length === 0) throw new Error("learn requires title and content");
    console.log(
      JSON.stringify(
        await vaultFirstLearn({
          vaultPath: DEFAULT_VAULT_PATH,
          title,
          content: contentParts.join(" "),
        }),
        null,
        2,
      ),
    );
    return;
  }

  if (command === "write") {
    const [section, title, ...contentParts] = args;
    if (!section || !title || contentParts.length === 0) throw new Error("write requires section, title, and content");
    console.log(
      JSON.stringify(
        await writeVaultPage({
          vaultPath: DEFAULT_VAULT_PATH,
          section: section as never,
          title,
          content: contentParts.join(" "),
        }),
        null,
        2,
      ),
    );
    return;
  }

  throw new Error(`Unknown command: ${command}`);
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : error);
  process.exit(1);
});
