#!/usr/bin/env ruby
# frozen_string_literal: true

# Deterministic incident handler for product repos. Writes a report only.
# Does not call production, Better Stack, or an agent.
# Usage:
#   ruby script/incident_handle.rb --payload tmp/incoming.json
#   ruby script/incident_handle.rb --fixture script/fixtures/incident.sample.json

require "json"
require "time"
require "fileutils"
require "optparse"

REQUIRED = %w[incident_id event].freeze

options = { out_dir: "tmp" }
OptionParser.new do |opts|
  opts.banner = "Usage: incident_handle.rb [options]"
  opts.on("--payload PATH") { |v| options[:payload] = v }
  opts.on("--fixture PATH") { |v| options[:payload] = v }
  opts.on("--out-dir PATH") { |v| options[:out_dir] = v }
end.parse!

abort "Pass --payload PATH" unless options[:payload]

payload = JSON.parse(File.read(options[:payload]))
payload = payload.reject { |key, _| key.to_s.start_with?("_") }
route = payload["route"].is_a?(Hash) ? payload.fetch("route") : {}
payload = payload.fetch("incident") if payload["incident"].is_a?(Hash)
REQUIRED.each do |key|
  abort "Missing required field: #{key}" if payload[key].to_s.strip.empty?
end

repo = ENV.fetch("GITHUB_REPOSITORY", "local")
FileUtils.mkdir_p(options[:out_dir])

report = {
  "source" => payload["source"] || "betterstack",
  "event" => payload["event"],
  "incident_id" => payload["incident_id"],
  "incident_name" => payload["incident_name"],
  "incident_url" => payload["incident_url"],
  "cause" => payload["cause"],
  "started_at" => payload["started_at"],
  "monitor_id" => payload["monitor_id"],
  "monitor_name" => payload["monitor_name"],
  "system" => payload["system"],
  "severity" => payload["severity"],
  "idempotency_key" => payload["idempotency_key"] || "betterstack:#{payload["incident_id"]}:#{payload["event"]}",
  "routed_project" => route["project"] || payload["routed_project"],
  "routed_repo" => route["repo"] || payload["routed_repo"],
  "handled_repo" => repo,
  "handled_at" => Time.now.utc.iso8601,
  "agent" => false
}

json_path = File.join(options[:out_dir], "incident-report.json")
md_path = File.join(options[:out_dir], "incident-report.md")
File.write(json_path, JSON.pretty_generate(report))
File.write(md_path, <<~MD)
  # Incident #{report["incident_id"]}

  - **Repo:** #{repo}
  - **Event:** #{report["event"]}
  - **Name:** #{report["incident_name"]}
  - **System:** #{report["system"]}
  - **Monitor:** #{report["monitor_name"]}
  - **Cause:** #{report["cause"]}
  - **URL:** #{report["incident_url"]}
  - **Started:** #{report["started_at"]}
  - **Idempotency key:** #{report["idempotency_key"]}
  - **Agent:** no (deterministic report only)

  This job does not acknowledge, resolve, deploy, or open production.
MD

puts "wrote #{md_path}"
