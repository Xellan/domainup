#!/usr/bin/env bash
# Bootstrap Python, then start the interactive installer.
set -euo pipefail

install_dependencies() {
    local family="$1"
    case "$family" in
        *debian*|*ubuntu*)
            apt-get update
            apt-get install -y python3 ca-certificates dnsutils curl
            ;;
        *fedora*|*rhel*|*centos*)
            dnf install -y python3 ca-certificates bind-utils curl
            ;;
        *)
            printf 'Автоустановка поддерживает Debian/Ubuntu и Fedora/RHEL (dnf). Система: %s\n' "$family" >&2
            return 1
            ;;
    esac
}

main() {
    if [[ "${1:-}" == "--help" ]]; then
        printf 'Использование: sudo bash install.sh\nИнтерактивная установка Selectel DDNS 2.0 на Linux с systemd >= 247.\n'
        return 0
    fi
    if (( $# )); then
        printf 'Неизвестный аргумент. Используйте --help.\n' >&2
        return 2
    fi
    if [[ ! -t 0 || ! -t 1 ]]; then
        printf 'Нужен интерактивный терминал. Скачайте репозиторий и запустите bash install.sh.\n' >&2
        return 1
    fi
    local source_dir
    source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
    if (( EUID != 0 )); then
        if ! command -v sudo >/dev/null; then
            printf 'Запустите установщик от root.\n' >&2
            return 1
        fi
        exec sudo bash "$source_dir/install.sh"
    fi
    if [[ ! -d /run/systemd/system ]] || ! command -v systemctl >/dev/null; then
        printf 'Нужна система с работающим systemd. Запустите установщик на хосте/VM с systemd.\n' >&2
        return 1
    fi
    local version
    version="$(systemctl --version | head -n 1)"
    version="${version#systemd }"
    version="${version%% *}"
    if [[ ! "$version" =~ ^[0-9]+$ ]] || (( version < 247 )); then
        printf 'Нужен systemd >= 247. Обновите ОС перед установкой.\n' >&2
        return 1
    fi
    local name
    for name in installer.py selectel-ddns.py selectel-ddns.service selectel-ddns.timer; do
        if [[ ! -f "$source_dir/$name" ]]; then
            printf 'Не хватает файла %s. Скачайте весь репозиторий.\n' "$name" >&2
            return 1
        fi
    done
    if [[ ! -r /etc/os-release ]]; then
        printf 'Не удалось определить ОС (/etc/os-release).\n' >&2
        return 1
    fi
    # Standard, root-owned operating system metadata.
    . /etc/os-release
    printf 'Установка зависимостей для %s…\n' "${PRETTY_NAME:-Linux}"
    install_dependencies "${ID:-} ${ID_LIKE:-}"
    if ! /usr/bin/python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
        printf 'Нужен Python >= 3.9. Обновите ОС перед установкой.\n' >&2
        return 1
    fi
    exec /usr/bin/python3 "$source_dir/installer.py"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
