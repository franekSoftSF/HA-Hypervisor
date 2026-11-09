FROM debian:12
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends systemd systemd-sysv dbus ca-certificates adduser
CMD ["/lib/systemd/systemd"]
