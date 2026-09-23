import { Container, getContainer } from "@cloudflare/containers";

// Lop nay dai dien cho "bo dieu khien" container chua bot Discord cua ban
export class BotContainer extends Container {
  defaultPort = 8080;   // phai khop voi cong health-check trong bot.py
  sleepAfter = "24h";   // gan nhu khong bao gio tu ngu, min duoc cron "choc" thuong xuyen

  constructor(ctx, env) {
    super(ctx, env);
    // Chuyen secret/bien moi truong ban dat trong Cloudflare dashboard vao trong container
    this.envVars = {
      DISCORD_TOKEN: env.DISCORD_TOKEN || "",
      GUILD_ID: env.GUILD_ID || "",
      CHANNEL_ID: env.CHANNEL_ID || "",
      SHOWCASE_CHANNEL_ID: env.SHOWCASE_CHANNEL_ID || "",
    };
  }
}

export default {
  // Neu co ai vo tinh mo URL cua Worker, chi can khoi dong/kiem tra container roi tra ve OK
  async fetch(request, env) {
    const container = getContainer(env.BOT_CONTAINER, "main");
    return container.fetch(request);
  },

  // Cron trigger: goi dinh ky de dam bao container luon dang chay (khong bi ngu)
  async scheduled(controller, env, ctx) {
    const container = getContainer(env.BOT_CONTAINER, "main");
    ctx.waitUntil(container.fetch(new Request("http://internal/")));
  },
};
