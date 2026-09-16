# Chatwoot, configured for the support agent (T-026). Run by compose's
# chatwoot-setup after migrations, and safe to run again: every record is found
# or created, then updated to match, so changing a value here and re-running
# converges rather than duplicating.
#
# What it makes, and why each setting is what it is:
#
#   account "Reference store", and an administrator to log into the desk
#   a website inbox with **hmac_mandatory**: a contact is accepted only with an
#     identifier and its HMAC, which only the portal can compute (T-026)
#   the agent bot "support-agent": its webhook URL, its signing secret and its
#     API token, all fixed from the environment so .env.example can name them
#   the bot attached to the inbox, so a new conversation starts with the agent
#     and moves to a person when the agent hands it off
#
# Tokens are set explicitly rather than generated, which Chatwoot would
# otherwise do, so the agent's settings are known before Chatwoot first starts.

require 'json'

def env!(name)
  value = ENV.fetch(name, '')
  raise "#{name} is required" if value.empty?

  value
end

account = Account.find_or_create_by!(name: 'Reference store')

email = env!('CHATWOOT_ADMIN_EMAIL')
admin = User.find_by(email: email) || User.new(email: email, name: 'Desk admin')
admin.password = env!('CHATWOOT_ADMIN_PASSWORD')
admin.password_confirmation = admin.password
admin.confirmed_at ||= Time.current
admin.save!
AccountUser.find_or_create_by!(account: account, user: admin) { |member| member.role = :administrator }

channel = Channel::WebWidget.find_by(website_token: env!('CHATWOOT_WEBSITE_TOKEN')) ||
          Channel::WebWidget.new(account: account)
channel.assign_attributes(
  website_url: env!('PORTAL_URL'),
  website_token: env!('CHATWOOT_WEBSITE_TOKEN'),
  hmac_token: env!('CHATWOOT_HMAC_TOKEN'),
  hmac_mandatory: true,
  continuity_via_email: false,
  widget_color: '#1f6feb'
)
channel.save!
inbox = Inbox.find_by(channel: channel) || Inbox.create!(account: account, name: 'Support', channel: channel)

bot = AgentBot.find_by(account_id: account.id, name: 'support-agent') ||
      AgentBot.new(account_id: account.id, name: 'support-agent')
bot.outgoing_url = env!('AGENT_WEBHOOK_URL')
bot.secret = env!('CHATWOOT_BOT_SECRET')
bot.save!
bot.access_token.update!(token: env!('CHATWOOT_BOT_TOKEN'))

link = AgentBotInbox.find_or_initialize_by(inbox: inbox, account: account)
link.agent_bot = bot
link.status = :active
link.save!

puts JSON.generate(account_id: account.id, inbox_id: inbox.id, agent_bot_id: bot.id)
