from glob import glob

rule setup:
    """프로파일 인프라 설치."""
    output: "temp/{profile}/setup.json"
    shell:
        """
        cd deploy/{wildcards.profile}
        TF_VAR_private_key=$KFKTEST_SSH_PKEY terraform apply -var-file=test.tfvars -auto-approve
        terraform output -json > ../../{output}
        """


rule destroy:
    """프로파일 인프라 제거."""
    output:
        "temp/{profile}/destroy"
    shell:
        """
        cd deploy/{wildcards.profile}
        TF_VAR_private_key=$KFKTEST_SSH_PKEY terraform destroy -var-file=test.tfvars -auto-approve
        cd ../..
        rm -f temp/{wildcards.profile}/setup.json
        touch {output}
        """


rule test_db:
    """Kafka 없이 DB 만 테스트."""
    input:
        "temp/{profile}/setup.json"
    output:
        "temp/{profile}/{epoch}/test_db"
    shell:
        """
        cd tests && pytest test_{wildcards.profile}.py::test_db -s | grep "per seconds" > ../{output}
        """


rule test_ct:
    """CT 테스트."""
    input:
        "temp/{profile}/setup.json"
    output:
        "temp/{profile}/{epoch}/test_ct"
    shell:
        """
        cd tests && pytest test_{wildcards.profile}.py::test_ct_remote_basic -s | grep "per seconds" > ../{output}
        """


rule test_cdc:
    """CDC 테스트."""
    input:
        "temp/{profile}/setup.json"
    output:
        "temp/{profile}/{epoch}/test_cdc"
    shell:
        """
        cd tests && pytest test_{wildcards.profile}.py::test_cdc_remote_basic -s | grep "per seconds" > ../{output}
        """


rule merge:
    """테스트 에포크 결과 결합.

    한 번에 하나의 테스트만 실행되도록 -j 1 으로 실행

    """
    input:
        "temp/{profile}/{epoch}/test_db",
        "temp/{profile}/{epoch}/test_ct",
        "temp/{profile}/{epoch}/test_cdc"
    output:
        "temp/{profile}/{epoch}/merge.parquet"
    script:
        "merge.py"


def _plot_input(wc):
    profile = wc[0]
    files = glob(f'temp/{profile}/*/merge.parquet')
    return files


rule plot:
    """모든 에포크 결과 모아 그리기.

    - 수동으로 실행된 에포크의 결과를 모아 그리는 경우
        - 먼저 각 에포크의 merge.parquet 를 수동으로 생성한 후
        - _plot_input 을 이용
    - 주어진 범위의 에포크를 자동으로 실행하여 그리는 경우
        - expand 를 이용하되, range 에 에포크 범위를 지정
        - -j 1 으로 한 번에 하나씩만 실행되도록 한다.

    """
    input:
        # lambda wc: _plot_input(wc)
        expand("temp/{{profile}}/{epoch}/merge.parquet", epoch=range(1,10))
    output:
        "temp/{profile}/plot.png"
    script:
        "plot.py"