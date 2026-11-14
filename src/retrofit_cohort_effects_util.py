"""工具函数：月份与日期运算、确定性JSON、基础数值与线性代数。

所有函数保持确定性：相同输入必得相同输出，不依赖时钟与随机源。
"""
from __future__ import annotations
import hashlib
import json
import math
from datetime import date


def parse_month(text):
    """解析YYYY-MM月份，返回(年, 月)二元组。"""
    if not isinstance(text, str) or len(text) != 7 or text[4] != "-":
        raise ValueError("月份格式应为YYYY-MM：" + repr(text))
    year, month = text[:4], text[5:7]
    if not (year.isdigit() and month.isdigit()):
        raise ValueError("月份格式应为YYYY-MM：" + repr(text))
    value = int(month)
    if not 1 <= value <= 12:
        raise ValueError("月份超出范围：" + repr(text))
    return int(year), value


def month_add(month, offset):
    """月份加减整数月。"""
    year, value = parse_month(month)
    total = year * 12 + (value - 1) + offset
    if total < 0:
        raise ValueError("月份偏移超出范围：" + repr(month))
    return "%04d-%02d" % (total // 12, total % 12 + 1)


def month_diff(a, b):
    """a与b相差的整月数（a - b）。"""
    ya, ma = parse_month(a)
    yb, mb = parse_month(b)
    return (ya - yb) * 12 + (ma - mb)


def check_date(text):
    """校验YYYY-MM-DD日期，返回date对象。"""
    if not isinstance(text, str) or len(text) != 10 or text[4] != "-" or text[7] != "-":
        raise ValueError("日期格式应为YYYY-MM-DD：" + repr(text))
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError("无效日期：" + repr(text)) from None


def month_end(month):
    """该月最后一天的日期串。"""
    year, value = parse_month(month)
    if value == 12:
        last = 31
    else:
        last = (date(year, value + 1, 1) - date(year, value, 1)).days
    return "%04d-%02d-%02d" % (year, value, last)


def canonical(obj):
    """确定性JSON序列化：键排序、无空白。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint_of(obj):
    """对象的确定性指纹（SHA-256）。"""
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def is_number(value):
    """有限数值（排除布尔）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def clean(obj, digits=12):
    """递归整理报告数值：非有限数转None，浮点保留固定精度。"""
    if isinstance(obj, float):
        if not math.isfinite(obj):
            return None
        return round(obj, digits)
    if isinstance(obj, list):
        return [clean(item, digits) for item in obj]
    if isinstance(obj, dict):
        return {key: clean(value, digits) for key, value in obj.items()}
    return obj


def mean(values):
    values = list(values)
    if not values:
        raise ValueError("无法对空序列取均值")
    return sum(values) / len(values)


def sample_sd(values):
    """样本标准差（ddof=1）；单点或空序列返回0。"""
    values = list(values)
    if len(values) < 2:
        return 0.0
    center = mean(values)
    return math.sqrt(sum((v - center) ** 2 for v in values) / (len(values) - 1))


def percentile(sorted_values, q):
    """有序序列的分位数（线性插值）。"""
    values = list(sorted_values)
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    pos = q * (len(values) - 1)
    low = int(math.floor(pos))
    high = int(math.ceil(pos))
    if low == high:
        return values[low]
    frac = pos - low
    return values[low] * (1 - frac) + values[high] * frac


def solve_linear(matrix, vector):
    """高斯消元（部分主元）解线性方程组。"""
    size = len(matrix)
    aug = [list(matrix[i]) + [vector[i]] for i in range(size)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            raise ValueError("线性方程组奇异，无法求解")
        aug[col], aug[pivot] = aug[pivot], aug[col]
        for row in range(col + 1, size):
            factor = aug[row][col] / aug[col][col]
            for c in range(col, size + 1):
                aug[row][c] -= factor * aug[col][c]
    result = [0.0] * size
    for i in range(size - 1, -1, -1):
        rest = sum(aug[i][j] * result[j] for j in range(i + 1, size))
        result[i] = (aug[i][size] - rest) / aug[i][i]
    return result


def sigmoid(z):
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    ez = math.exp(z)
    return ez / (1.0 + ez)


def _log_likelihood(features, labels, beta, ridge):
    """带岭惩罚的对数似然，数值稳定。"""
    total = 0.0
    for x, y in zip(features, labels):
        z = sum(b * xi for b, xi in zip(beta, x))
        if z >= 0:
            total += y * z - z - math.log1p(math.exp(-z))
        else:
            total += y * z - math.log1p(math.exp(z))
    return total - 0.5 * ridge * sum(b * b for b in beta)


def fit_logistic(features, labels, ridge=1e-6, max_iter=100, tol=1e-10):
    """牛顿法配步长折半线搜索拟合逻辑回归，返回(系数, 是否收敛)。

    分离数据下似然没有有限极大值，线搜索保证每步似然不减，
    系数会持续增大直到迭代上限，此时返回converged=False供上层诊断。
    """
    width = len(features[0])
    beta = [0.0] * width
    converged = False
    current = _log_likelihood(features, labels, beta, ridge)
    for _ in range(max_iter):
        grad = [0.0] * width
        hess = [[0.0] * width for _ in range(width)]
        for x, y in zip(features, labels):
            p = sigmoid(sum(b * xi for b, xi in zip(beta, x)))
            w = max(p * (1.0 - p), 1e-10)
            resid = y - p
            for i in range(width):
                grad[i] += resid * x[i]
                for j in range(width):
                    hess[i][j] += w * x[i] * x[j]
        for i in range(width):
            hess[i][i] += ridge
            grad[i] -= ridge * beta[i]
        step = solve_linear(hess, grad)
        scale = 1.0
        accepted = False
        for _ in range(30):
            candidate = [b + scale * s for b, s in zip(beta, step)]
            value = _log_likelihood(features, labels, candidate, ridge)
            if value >= current:
                accepted = True
                break
            scale *= 0.5
        if not accepted:
            break
        beta = candidate
        if value - current < tol:
            converged = True
            current = value
            break
        current = value
    return beta, converged
